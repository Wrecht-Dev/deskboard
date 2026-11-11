"""Convert Tumblr's NPF text formatting into valid Pango markup.

Tumblr hands us a *flat* list of formatting ranges.  Ranges may overlap
(``bold 0..10`` + ``italic 5..15``), nest, be empty, run backwards, or point
past the end of the string.  Wrapping them directly produces markup like
``<b>abc<i>def</b>ghi</i>`` which Pango rejects (and, worse, makes GTK log a
critical and render nothing).  Instead we slice the text at every range
boundary and emit a fully balanced tag chain for each slice.  The extra tags
cost nothing at render time and make broken input impossible to emit.
"""
from __future__ import annotations

import ast
import re
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Escaping
# --------------------------------------------------------------------------- #

# Pango's markup parser understands exactly the five predefined XML entities.
_ESCAPES = {
    0x26: "&amp;",   # &
    0x3C: "&lt;",    # <
    0x3E: "&gt;",    # >
    0x22: "&quot;",  # "
    0x27: "&apos;",  # '
}

# Control characters are not legal in Pango text and make markup parsing fail.
_CONTROL = {code: None for code in range(0x00, 0x09)}
_CONTROL.update({code: None for code in range(0x0B, 0x0D)})  # keep \n and \t
_CONTROL.update({code: None for code in range(0x0E, 0x20)})
_CONTROL[0x7F] = None
_CONTROL[0x0D] = "\n"

_HEX_COLOR = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_HTTP_URL = re.compile(r"^(?:https?://|//)[^\s<>\"']+$", re.IGNORECASE)
_WWW_URL = re.compile(r"^www\.[^\s<>\"']+$", re.IGNORECASE)
_HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_BOLD = "bold"
_ITALIC = "italic"
_STRIKE = "strikethrough"
_LINK = "link"
_MENTION = "mention"
_COLOR = "color"

_KNOWN = {_BOLD, _ITALIC, _STRIKE, _LINK, _MENTION, _COLOR}


def escape_markup(text: str) -> str:
    """Escape text for Pango markup, keeping newlines/tabs intact."""
    if not text:
        return ""
    return str(text).translate(_ESCAPES).translate(_CONTROL)


def _field(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _clean_url(url: str) -> str:
    """Return an anchor-safe absolute URL, or ``""`` when unusable."""
    url = (url or "").strip()
    if not url or len(url) > 2048:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif _WWW_URL.match(url):
        url = "https://" + url
    if not _HTTP_URL.match(url):
        return ""
    return escape_markup(url)


def _mention_url(fmt: Any, visible: str, text: str) -> str:
    """Work out where a ``mention`` range should point.

    Newer payloads carry ``blog`` as a stringified dict (a quirk of the model
    layer); older ones only have the visible ``@name``.  Both are handled and a
    missing target simply renders as styled text.
    """
    raw = _field(fmt, "blog", "") or ""
    if isinstance(raw, dict):
        direct = str(raw.get("url") or "")
        name = str(raw.get("name") or "")
        if direct:
            return _clean_url(direct)
        if _HANDLE.match(name):
            return f"https://www.tumblr.com/{name}"
    elif isinstance(raw, str) and raw.strip().startswith("{") and len(raw) < 4096:
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, dict):
            direct = str(parsed.get("url") or "")
            name = str(parsed.get("name") or "")
            if direct:
                return _clean_url(direct)
            if _HANDLE.match(name):
                return f"https://www.tumblr.com/{name}"
    elif isinstance(raw, str) and _HANDLE.match(raw.strip()):
        return f"https://{raw.strip()}.tumblr.com"

    url = _clean_url(_field(fmt, "url", "") or "")
    if url:
        return url

    candidate = (visible or "").strip()
    if not candidate:
        span = text[_coerce_int(_field(fmt, "start", 0)): _coerce_int(_field(fmt, "end", 0))]
        candidate = span.strip()
    candidate = candidate.lstrip("@").strip()
    if _HANDLE.match(candidate):
        return f"https://www.tumblr.com/{candidate}"
    return ""


# --------------------------------------------------------------------------- #
# Range normalisation
# --------------------------------------------------------------------------- #

def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalise(text: str, formatting: Iterable[Any] | None) -> list[dict]:
    """Clamp formatting ranges to the text and drop unusable entries."""
    length = len(text)
    spans: list[dict] = []
    for fmt in formatting or []:
        try:
            kind = str(_field(fmt, "type", "") or "").strip().lower()
        except Exception:  # noqa: BLE001 - malformed foreign object
            continue
        if kind not in _KNOWN:
            continue
        start = max(0, min(length, _coerce_int(_field(fmt, "start", 0), 0)))
        end = max(0, min(length, _coerce_int(_field(fmt, "end", start), start)))
        if end <= start:
            continue
        span = {
            "start": start,
            "end": end,
            "kind": kind,
            "url": "",
            "color": "",
        }
        if kind in (_LINK, _MENTION):
            visible = text[start:end]
            span["url"] = (
                _mention_url(fmt, visible, text) if kind == _MENTION
                else _clean_url(_field(fmt, "url", "") or "")
            )
        elif kind == _COLOR:
            raw = str(_field(fmt, "hex", "") or "").strip()
            if _HEX_COLOR.match(raw):
                span["color"] = raw if raw.startswith("#") else "#" + raw
        spans.append(span)
    spans.sort(key=lambda item: (item["start"], -item["end"]))
    return spans


# --------------------------------------------------------------------------- #
# Markup generation
# --------------------------------------------------------------------------- #

def _open_tags(active: list[dict]) -> list[str]:
    """Balanced opening tags for the formats covering the current slice.

    One attribute of each kind is chosen deterministically (innermost range
    wins, because the list is sorted by start), so overlapping ranges can
    never emit two anchors or two conflicting foreground colours.
    """
    anchor_url = ""
    foreground = ""
    bold = italic = strike = False
    underline = False

    for span in active:
        kind = span["kind"]
        if kind in (_LINK, _MENTION):
            underline = True
            if span["url"]:
                anchor_url = span["url"]
        elif kind == _COLOR:
            foreground = span["color"] or foreground
        elif kind == _BOLD:
            bold = True
        elif kind == _ITALIC:
            italic = True
        elif kind == _STRIKE:
            strike = True

    tags: list[str] = []
    if anchor_url:
        # Gtk.Label accepts Pango's anchor extension (plain Pango.parse_markup
        # does not); GTK themes the colour and emits ``activate-link``.
        tags.append(f'<a href="{anchor_url}">')
    if foreground:
        tags.append(f'<span foreground="{foreground}">')
    elif underline and not anchor_url:
        tags.append('<span underline="single">')
    if bold:
        tags.append("<b>")
    if italic:
        tags.append("<i>")
    if strike:
        tags.append("<s>")
    return tags


def _close_tags(active: list[dict]) -> list[str]:
    tags = _open_tags(active)
    closing: list[str] = []
    for tag in reversed(tags):
        if tag.startswith("<a "):
            closing.append("</a>")
        elif tag.startswith("<span"):
            closing.append("</span>")
        elif tag == "<b>":
            closing.append("</b>")
        elif tag == "<i>":
            closing.append("</i>")
        elif tag == "<s>":
            closing.append("</s>")
    return closing


def to_pango(text: str, formatting: Iterable[Any] | None = None) -> str:
    """Render ``text`` plus its flat formatting ranges as Pango markup.

    The returned string is always well-formed markup: the text is escaped
    first, then split at every formatting boundary so tags nest cleanly.
    """
    if not text:
        return ""

    text = str(text)
    spans = _normalise(text, formatting)

    # Escape the text in pieces later, but compute segment text from the raw
    # string so offsets keep their meaning.
    if not spans:
        return escape_markup(text)

    boundaries = {0, len(text)}
    for span in spans:
        boundaries.add(span["start"])
        boundaries.add(span["end"])
    points = sorted(boundaries)

    out: list[str] = []
    for index in range(len(points) - 1):
        begin, finish = points[index], points[index + 1]
        if finish <= begin:
            continue
        active = [span for span in spans if span["start"] <= begin and span["end"] >= finish]
        chunk = escape_markup(text[begin:finish])
        if not active:
            out.append(chunk)
            continue
        out.append("".join(_open_tags(active)))
        out.append(chunk)
        out.append("".join(_close_tags(active)))
    return "".join(out)


# --------------------------------------------------------------------------- #
# Plain text flattening (notifications, window titles, previews)
# --------------------------------------------------------------------------- #

def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _raw_data(block: Any) -> dict:
    data = _get(block, "data", None)
    if isinstance(data, dict):
        return data
    return block if isinstance(block, dict) else {}


def _block_text(block: Any) -> str:
    text = _get(block, "text", "") or ""
    if text:
        return str(text)
    data = _raw_data(block)
    for key in ("text", "answerText", "questionText"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _nested_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return plain_text(value)
    return ""


def _flatten_block(block: Any) -> str:
    kind = str(_get(block, "type", "") or "").lower()
    data = _raw_data(block)

    if kind == "poll":
        question = str(data.get("question") or "").strip()
        answers = [
            str(item.get("answerText") or item.get("text") or "").strip()
            for item in data.get("answers") or []
            if isinstance(item, dict)
        ]
        parts = [question] + [answer for answer in answers if answer]
        return "\n".join(part for part in parts if part)

    if kind == "answer":
        question = _nested_text(data.get("question"))
        answer = _nested_text(data.get("answer"))
        parts = [question, answer]
        body = "\n".join(part for part in parts if part)
        if body:
            return body

    if kind == "link":
        title = str(_get(block, "title", "") or data.get("title") or "").strip()
        description = str(_get(block, "description", "") or data.get("description") or "").strip()
        url = str(_get(block, "url", "") or data.get("url") or "").strip()
        parts = [title, description, url]
        return "\n".join(part for part in parts if part)

    text = _block_text(block).strip()
    if text:
        return text

    # Media-only blocks: an alt text is by far the most useful preview.
    media = _get(block, "media", None) or []
    if isinstance(media, list):
        for item in media:
            alt = str(_get(item, "alt", "") or "").strip()
            if alt:
                return alt
    labels = {"image": "Photo", "video": "Video", "audio": "Audio"}
    if kind in labels:
        return labels[kind]
    return ""


def plain_text(blocks: Iterable[Any]) -> str:
    """Flatten blocks into readable plain text (used for previews/labels)."""
    parts: list[str] = []
    seen: set[str] = set()
    for block in blocks or []:
        try:
            text = _flatten_block(block)
        except Exception:  # noqa: BLE001 - a weird block must not break a preview
            continue
        text = text.strip()
        if not text:
            continue
        # Legacy answer payloads repeat the answer as a following text block.
        if text in seen:
            continue
        seen.add(text)
        parts.append(text)
    joined = "\n\n".join(parts)
    joined = re.sub(r"[ \t]+\n", "\n", joined)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined.strip()
