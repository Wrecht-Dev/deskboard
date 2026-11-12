"""Notes helpers for the post-detail page.

The notes endpoint (``GET /v2/blog/{blog}/notes``) returns untyped dictionaries
under ``response.notes``; this module keeps everything that does **not** need
GTK in one place so the widget can stay about layout and lifecycle:

* note-dict normalisation (actor, action wording, avatar, timestamp, reply),
* raw note timestamps for note-targeting writes,
* deduplication keys across page boundaries,
* the two pagination styles the endpoint has used.

Verified live shape (public staff post, 2026-11)::

    {"response": {
        "notes": [
            {"type": "like", "blogName": "…", "blogTitle": "…",
             "blogUrl": "https://….tumblr.com/", "timestamp": 1794421864,
             "followed": false,
             "avatarUrl": {"64": "https://…", "128": "https://…"}},
            {"type": "reply", "replyText": "…", "formatting": […]},
            {"type": "reblog", "postId": "832620989192060928"},
        ],
        "links": {"next": {"href": "/v2/blog/staff/notes?…&cursor=…"}},
        "totalNotes": 46471,
    }}

Pagination note: the agreed client surface forwards a ``beforeTimestamp`` token
through ``get_notes(..., before=…)``, but the live endpoint currently hands back
an opaque ``cursor``.  :func:`load_notes_page` supports both so either contract
keeps working; callers never have to know which one they got.
"""
from __future__ import annotations

import inspect
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Note ``type`` → past-tense action shown next to the actor.
_VERBS = {
    "like": "liked",
    "reblog": "reblogged",
    "reply": "replied",
    "follow": "followed",
    "answer": "answered",
}


@dataclass
class NotePage:
    """One page of notes, independent of the API's transport object."""

    items: list[dict[str, Any]] = field(default_factory=list)
    next_url: str | None = None
    total: int | None = None

    def __bool__(self) -> bool:
        return bool(self.items)


# --------------------------------------------------------------------------- #
# note-dict accessors
# --------------------------------------------------------------------------- #

def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int(value: Any) -> int:
    try:
        if isinstance(value, bool):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def note_type(note: Any) -> str:
    """The normalised note kind (``like`` when the field is missing)."""
    if not isinstance(note, dict):
        return "like"
    kind = _text(note.get("type") or note.get("postType")).lower()
    return kind or "like"


def note_verb(note: Any) -> str:
    """Past-tense verb for the action line: ``liked``, ``reblogged``…"""
    kind = note_type(note)
    return _VERBS.get(kind, kind or "noted")


def note_blog_name(note: Any) -> str:
    if not isinstance(note, dict):
        return ""
    return _text(note.get("blogName"))


def note_display_name(note: Any) -> str:
    """Prefer the blog title; fall back to the blog name.

    Tons of Tumblr blogs call themselves "Untitled", which is noise in a compact
    notes row, so that placeholder falls through to the blog name too.
    """
    if not isinstance(note, dict):
        return ""
    title = _text(note.get("blogTitle"))
    name = note_blog_name(note)
    if title and title.lower() not in ("untitled", "unknown"):
        return title
    return name


def note_timestamp(note: Any) -> int:
    if not isinstance(note, dict):
        return 0
    return _int(note.get("timestamp"))


def note_followed(note: Any) -> bool:
    if not isinstance(note, dict):
        return False
    return bool(note.get("followed"))


def note_created_time(note: Any) -> str:
    """The raw note timestamp for note-targeting writes, precision preserved.

    Replies from the live endpoint carry fractional timestamps
    (``1794256896.868151``), so callers must not round this through
    :func:`note_timestamp`; ``delete_note``/``flag_note`` need exactly the value
    the notes payload supplied.  Returns "" when there is no usable timestamp.
    """
    if not isinstance(note, dict):
        return ""
    value = note.get("timestamp")
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            if float(value) <= 0:
                return ""
        except (TypeError, ValueError, OverflowError):
            return ""
        return str(value)
    text = _text(value)
    return "" if text in ("", "0") else text


def note_reply_text(note: Any) -> str:
    """The body of a ``reply`` note (empty for every other kind)."""
    if not isinstance(note, dict):
        return ""
    return _text(note.get("replyText"))


def note_formatting(note: Any) -> list[Any]:
    """Flat Pango formatting ranges attached to a reply, when present."""
    if not isinstance(note, dict):
        return []
    value = note.get("formatting")
    return list(value) if isinstance(value, list) else []


def _avatar_from_list(value: Iterable[Any], size: int) -> str:
    best = ""
    best_width = -1
    for entry in value:
        if isinstance(entry, str) and entry:
            url = entry
            width = size
        elif isinstance(entry, dict):
            url = _text(entry.get("url"))
            width = _int(entry.get("width"))
        else:
            continue
        if not url:
            continue
        if width >= size:
            return url
        if width > best_width:
            best, best_width = url, width
    return best


def note_avatar_url(note: Any, size: int = 64) -> str:
    """Pull the best avatar URL out of the several shapes notes have used.

    Live notes use ``{"64": url, "128": url}``; older payloads used a bare
    string and some routes use ``[{"width": …, "url": …}]``.  All are accepted.
    """
    if not isinstance(note, dict):
        return ""
    value = note.get("avatarUrl")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        preferred = [str(size), "128", "64"]
        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for candidate in value.values():
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""
    if isinstance(value, (list, tuple)):
        return _avatar_from_list(value, int(size))
    return ""


def note_key(note: Any) -> str:
    """Stable identity used to drop duplicates when pages overlap."""
    if not isinstance(note, dict):
        return f"object|{id(note)}"
    return "|".join(
        (
            note_type(note),
            note_blog_name(note),
            str(note_timestamp(note)),
            _text(note.get("postId")),
            note_reply_text(note)[:48],
        )
    )


# --------------------------------------------------------------------------- #
# pagination
# --------------------------------------------------------------------------- #

def query_value(url: Any, *names: str) -> str | None:
    """Return the first non-empty query value in ``url`` for ``names``."""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(str(url or "")).query)
    except (TypeError, ValueError):
        return None
    for name in names:
        values = query.get(name)
        if values:
            for value in values:
                if value:
                    return str(value)
    return None


def next_before(next_url: Any) -> str | None:
    """A ``beforeTimestamp`` / ``before`` token embedded in ``next_url``."""
    return query_value(next_url, "beforeTimestamp", "before")


def next_cursor(next_url: Any) -> str | None:
    """The opaque cursor token the live notes route currently uses."""
    return query_value(next_url, "cursor")


# --------------------------------------------------------------------------- #
# page conversion / loading
# --------------------------------------------------------------------------- #

def _first_int(mapping: Any, *keys: str) -> int | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping and mapping[key] is not None:
            value = _int(mapping[key])
            return value if value else None
    return None


def _items_from_mapping(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("notes", "items"):
        value = mapping.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _next_href(mapping: dict[str, Any]) -> str | None:
    links = mapping.get("links")
    if not isinstance(links, dict):
        return None
    nxt = links.get("next")
    if isinstance(nxt, dict):
        href = nxt.get("href")
        if isinstance(href, str) and href.strip():
            return href.strip()
    elif isinstance(nxt, str) and nxt.strip():
        return nxt.strip()
    return None


def page_from_payload(data: Any) -> NotePage:
    """Convert a raw API response (or a bare list) into :class:`NotePage`."""
    if isinstance(data, list):
        return NotePage(items=[item for item in data if isinstance(item, dict)])
    if not isinstance(data, dict):
        return NotePage()
    response: dict[str, Any] = data
    nested = data.get("response")
    if isinstance(nested, dict):
        response = nested
    items = _items_from_mapping(response)
    total = _first_int(response, "totalNotes", "total", "totalConversations")
    return NotePage(items=items, next_url=_next_href(response), total=total)


def page_from_item_page(page: Any) -> NotePage:
    """Convert ``client.get_notes``'s ``ItemPage`` (or a demo ``Page``)."""
    if page is None:
        return NotePage()
    items = [item for item in (getattr(page, "items", None) or []) if isinstance(item, dict)]
    next_url = getattr(page, "next_url", None)
    total = getattr(page, "total", None)
    raw = getattr(page, "raw", None)
    if total is None and isinstance(raw, dict):
        total = _first_int(raw, "totalNotes", "total")
    if not items and not next_url:
        # Legacy/demo clients answer with a ``Page`` of posts, never note dicts.
        return NotePage(total=total)
    return NotePage(
        items=items,
        next_url=next_url if isinstance(next_url, str) and next_url else None,
        total=_int(total) if total is not None else None,
    )


def _supports_blog_first(getter: Any) -> bool:
    """True when ``getter`` uses the agreed ``(blog, post_id, …)`` signature."""
    try:
        parameters = list(inspect.signature(getter).parameters)
    except (TypeError, ValueError):
        return True
    return bool(parameters) and parameters[0] in ("blog", "blog_name")


def _call_get_notes(client: Any, blog: str, post_id: str, mode: str,
                    before: str | None) -> NotePage:
    getter = getattr(client, "get_notes", None)
    if not callable(getter):
        return NotePage()
    if _supports_blog_first(getter):
        page = getter(blog, post_id, mode=mode, before=before)
    else:
        # DemoClient predates the notes endpoint: get_notes(post_id, blog="").
        page = getter(post_id, blog)
    return page_from_item_page(page)


def load_notes_page(
    client: Any,
    blog: str,
    post_id: str,
    next_url: str | None = None,
    mode: str = "all",
) -> NotePage:
    """Load one page of notes, following whatever pagination token we got.

    ``next_url`` is the ``ItemPage.next_url`` from the previous call (``None``
    for the first page).  A ``beforeTimestamp`` token is handed to
    ``get_notes(before=…)``; a cursor is followed through ``client.request``.
    """
    blog = _text(blog)
    post_id = _text(post_id)
    if not blog or not post_id:
        return NotePage()
    if next_url:
        before = next_before(next_url)
        if before:
            return _call_get_notes(client, blog, post_id, mode, before)
        request = getattr(client, "request", None)
        if callable(request):
            path = str(next_url)
            if not path.startswith(("http://", "https://", "/")):
                path = "/" + path
            return page_from_payload(request(path))
        # Unknown token style: hand it through the agreed ``before`` parameter.
        return _call_get_notes(client, blog, post_id, mode, str(next_url))
    return _call_get_notes(client, blog, post_id, mode, None)


def describe_error(error: BaseException) -> str:
    """A short, user-safe description for the inline error state."""
    message = str(error) or type(error).__name__
    message = " ".join(str(message).split())
    if len(message) > 300:
        message = message[:297].rstrip() + "…"
    return message
