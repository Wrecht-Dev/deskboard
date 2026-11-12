"""Composer media attachments and the Popular GIFs picker.

Owner: media.  Public surface for the shell composer:

    AttachTray(on_insert: Callable[[dict], None])

``on_insert`` receives one Tumblr **media object** ready to be embedded as
``{"type": "image", "media": [media]}``.  ``ctx.client.upload_image`` returns
the raw API envelope (``{"response": ...}``); this module unwraps it so the
composer never has to know about that detail.

The tray resolves the application context from the window that embeds it:
``get_root().ctx`` (the composer window sets this) or its transient parent.
Media is only uploaded after an explicit file-dialog click; GIFs are only
fetched when the picker is opened.
"""
from __future__ import annotations

import mimetypes
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from ..api.client import TumblrError

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # Tumblr rejects larger images server-side too
_THUMB_WIDTH = 320
_THUMB_HEIGHT = 240

_CSS_INSTALLED = False


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _install_css() -> None:
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    _CSS_INSTALLED = True
    try:
        path = Path(__file__).with_name("attach.css")
        if not path.is_file():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(path))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
    except Exception:  # noqa: BLE001 - styling must never break the composer
        pass


def _error_text(exc: BaseException, action: str = "upload") -> str:
    """Short, user-facing error text; never a traceback."""
    if isinstance(exc, TumblrError):
        status = int(getattr(exc, "status", 0) or 0)
        if status in (401, 403):
            return "Your Tumblr session expired — sign in again to attach media."
        if status == 0:
            if action == "gif":
                return "Couldn't load GIFs — check your connection and try again."
            return "Upload failed — check your connection and try again."
        message = str(exc).strip()
        if message:
            return message.splitlines()[0][:180]
        return "Tumblr rejected the request. Try again."
    if action == "gif":
        return "Couldn't load GIFs. Try again."
    return "Media request failed. Try again."


def _media_object(result: Any) -> dict | None:
    """Unwrap the upload envelope and return a usable NPF media object."""
    if not isinstance(result, dict) or not result:
        return None
    response = result.get("response")
    if isinstance(response, dict):
        nested = response.get("media")
        if isinstance(nested, dict) and nested:
            return dict(nested)
        if response:
            return dict(response)
        return None
    return dict(result)


def _gif_media(gif: Any) -> dict | None:
    """Pick the largest media rendition from one ``/v2/gif/popular`` item."""
    if not isinstance(gif, dict):
        return None
    media = gif.get("media")
    if isinstance(media, list):
        candidates = [item for item in media if isinstance(item, dict) and item.get("url")]
        if candidates:
            def area(item: dict) -> int:
                try:
                    return int(item.get("width") or 0) * int(item.get("height") or 0)
                except (TypeError, ValueError):
                    return 0
            return dict(max(candidates, key=area))
    if isinstance(media, dict) and media.get("url"):
        return dict(media)
    if gif.get("url") and (gif.get("width") or gif.get("height")):
        return {
            key: gif[key]
            for key in ("url", "width", "height", "type", "mediaKey", "poster", "video")
            if key in gif
        }
    return None


def _poster_url_from(source: Any) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        for item in reversed(source):
            url = _poster_url_from(item)
            if url:
                return url
    if isinstance(source, dict):
        return str(source.get("url") or "")
    return ""


def _gif_poster_url(gif: Any) -> str:
    if not isinstance(gif, dict):
        return ""
    media = _gif_media(gif) or {}
    for candidate in (media.get("poster"), gif.get("poster"), media.get("thumbnail")):
        url = _poster_url_from(candidate)
        if url:
            return url
    url = str(media.get("url") or "")
    if url.lower().split("?")[0].endswith((".gif", ".gifv", ".webp", ".png", ".jpg", ".jpeg")):
        return url
    return ""


def _gif_key(gif: Any) -> str:
    if not isinstance(gif, dict):
        return ""
    media = _gif_media(gif) or {}
    return str(
        media.get("mediaKey")
        or media.get("url")
        or gif.get("id")
        or ""
    )


def _nice_size(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


# --------------------------------------------------------------------------- #
# Popular GIFs dialog
# --------------------------------------------------------------------------- #

class _GifTile(Gtk.Overlay):
    """One thumbnail; loads only while mapped, reuses the image cache."""

    __gtype_name__ = "DeskboardGifTile"

    def __init__(self, poster_url: str, ctx: Any = None) -> None:
        super().__init__()
        self.poster_url = poster_url
        self.ctx = ctx
        self._alive = True
        self._loaded = False
        self._mapped = False
        self.set_hexpand(True)
        self.set_vexpand(False)
        self.set_size_request(150, 120)

        frame = Gtk.Box()
        frame.add_css_class("db-gif-tile")
        try:
            frame.set_overflow(Gtk.Overflow.HIDDEN)
        except Exception:  # noqa: BLE001
            pass
        self.set_child(frame)

        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.picture.set_can_shrink(True)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        self.picture.set_size_request(1, 1)
        frame.append(self.picture)

        badge = Gtk.Label(label="GIF")
        badge.add_css_class("db-gif-badge")
        badge.set_halign(Gtk.Align.START)
        badge.set_valign(Gtk.Align.START)
        badge.set_margin_top(6)
        badge.set_margin_start(6)
        badge.set_can_target(False)
        self.add_overlay(badge)

        try:
            if self.ctx is None:
                root = self.get_root()
                if root is not None:
                    self.ctx = getattr(root, "ctx", None)
            if self.ctx is not None:
                texture = self.ctx.images.cached(self.poster_url, _THUMB_WIDTH)
                if texture is not None:
                    self.picture.set_paintable(texture)
                    self._loaded = True
        except Exception:  # noqa: BLE001
            pass
        self.connect("map", self._on_map_signal)
        self.connect("unmap", self._on_unmap_signal)

    def _on_map_signal(self, *_args) -> None:
        self._mapped = True
        if self._loaded or not self.poster_url:
            return
        self._schedule_load()

    def _on_unmap_signal(self, *_args) -> None:
        self._mapped = False
        try:
            if self.picture.get_paintable() is not None:
                self.picture.set_paintable(None)
        except Exception:  # noqa: BLE001
            pass
        self._loaded = False

    def _schedule_load(self) -> None:
        def run() -> bool:
            if self._alive and self.get_mapped():
                self._load()
            return False

        GLib.idle_add(run)

    def _load(self) -> None:
        if self._loaded or not self._alive or not self.get_mapped():
            return
        ctx = self.ctx
        if ctx is None:
            root = self.get_root()
            ctx = getattr(root, "ctx", None) if root is not None else None
            self.ctx = ctx
        if ctx is None:
            return
        try:
            cached = ctx.images.cached(self.poster_url, _THUMB_WIDTH)
        except Exception:  # noqa: BLE001
            cached = None
        if cached is not None:
            self._loaded = True
            self.picture.set_paintable(cached)
            return
        self._loaded = True

        def apply(texture: Any) -> None:
            if not self._alive:
                return
            if texture is None or not self.get_mapped():
                return
            try:
                self.picture.set_paintable(texture)
            except Exception:  # noqa: BLE001
                pass

        try:
            ctx.images.load(self.poster_url, _THUMB_WIDTH, _THUMB_HEIGHT, apply)
        except Exception:  # noqa: BLE001
            pass


class _GifPickerDialog(Adw.Dialog):
    """Adw.Dialog grid of Tumblr's popular GIFs with offset pagination."""

    __gtype_name__ = "DeskboardGifPickerDialog"
    PAGE_SIZE = 20
    _NEAR_BOTTOM = 320

    def __init__(self, ctx: Any, on_insert: Callable[[dict], None],
                 parent: Optional[Gtk.Window] = None) -> None:
        super().__init__()
        _install_css()
        self.ctx = ctx
        self.on_insert = on_insert
        self._alive = True
        self._loading = False
        self._done = False
        self._offset = 0
        self._next_offset: int | None = 0
        self._failed_offset: int | None = None
        self._seen: set[str] = set()
        self._items: list[Gtk.Widget] = []
        self._vadj_handler = 0

        self.set_title("Popular GIFs")
        try:
            self.set_content_width(920)
            self.set_content_height(680)
        except Exception:  # noqa: BLE001
            pass
        self.add_css_class("db-gif-dialog")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Popular GIFs", subtitle="Tumblr"))
        close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close.add_css_class("flat")
        close.set_tooltip_text("Close (Esc)")
        close.connect("clicked", lambda *_a: self.close())
        header.pack_start(close)
        box.append(header)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_hexpand(True)
        self.scroller.set_vexpand(True)
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_homogeneous(True)
        self.flow.set_min_children_per_line(3)
        self.flow.set_max_children_per_line(5)
        self.flow.set_column_spacing(8)
        self.flow.set_row_spacing(8)
        self.flow.set_margin_top(12)
        self.flow.set_margin_bottom(12)
        self.flow.set_margin_start(12)
        self.flow.set_margin_end(12)
        self.scroller.set_child(self.flow)
        box.append(self.scroller)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.add_css_class("db-gif-footer")
        self.status_label = Gtk.Label(label="")
        self.status_label.add_css_class("db-gif-hint")
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_xalign(0)
        self.status_label.set_hexpand(True)
        footer.append(self.status_label)
        self.retry_button = Gtk.Button(label="Retry")
        self.retry_button.add_css_class("flat")
        self.retry_button.set_visible(False)
        self.retry_button.connect("clicked", lambda *_a: self._retry())
        footer.append(self.retry_button)
        box.append(footer)
        self.set_child(box)

        try:
            adjustment = self.scroller.get_vadjustment()
            self._vadj_handler = adjustment.connect("value-changed", self._on_scroll)
        except Exception:  # noqa: BLE001
            self._vadj_handler = 0
        try:
            self.connect("closed", self._on_closed)
        except Exception:  # noqa: BLE001
            pass

        self._load_page(0)
        try:
            self.present(parent)
        except TypeError:
            self.present()

    # -- data -------------------------------------------------------------- #
    def _set_status(self, text: str, error: bool = False) -> None:
        try:
            self.status_label.set_text(text)
            self.status_label.set_visible(bool(text))
            if error:
                self.status_label.add_css_class("db-attach-error")
            else:
                self.status_label.remove_css_class("db-attach-error")
        except Exception:  # noqa: BLE001
            pass

    def _load_page(self, offset: int | None) -> None:
        if self._loading or not self._alive or offset is None:
            return
        if self._next_offset is None and not self._items:
            return
        self._loading = True
        self._offset = int(offset or 0)
        self.retry_button.set_visible(False)
        self._set_status("Loading GIFs…")

        def work() -> None:
            try:
                gifs, next_offset = self.ctx.client.get_gif_popular(offset=offset)
                GLib.idle_add(self._apply, gifs, next_offset, None)
            except Exception as exc:  # noqa: BLE001 - surfaced inline
                GLib.idle_add(self._apply, [], None, exc)

        threading.Thread(target=work, daemon=True, name="gif-popular").start()

    def _apply(self, gifs: Any, next_offset: Any,
               error: BaseException | None) -> bool:
        if not self._alive:
            return False
        self._loading = False
        if error is not None:
            self._failed_offset = self._next_offset if self._next_offset not in (None, 0) else 0
            self._set_status(_error_text(error, action='gif'), error=True)
            self.retry_button.set_visible(True)
            return False
        self._failed_offset = None
        self.retry_button.set_visible(False)
        added = 0
        for gif in list(gifs or []):
            if not isinstance(gif, dict):
                continue
            key = _gif_key(gif)
            if key and key in self._seen:
                continue
            media = _gif_media(gif)
            poster = _gif_poster_url(gif)
            if not media or not poster:
                continue
            if key:
                self._seen.add(key)
            try:
                tile = _GifTile(poster, self.ctx)
                tile.set_size_request(160, 120)
                button = Gtk.Button()
                button.add_css_class("flat")
                button.add_css_class("db-gif-button")
                button.set_size_request(160, 120)
                button.set_hexpand(False)
                button.set_child(tile)
                button.set_tooltip_text("Insert this GIF")
                button.connect("clicked", lambda _b, item=media: self._choose(item))
                self.flow.insert(button, -1)
                self._items.append(button)
                added += 1
            except Exception:  # noqa: BLE001
                continue
        try:
            self._next_offset = int(next_offset) if next_offset is not None else None
        except (TypeError, ValueError):
            self._next_offset = None
        if self._next_offset is not None and self._next_offset <= self._offset:
            # Defensive: a non-advancing offset would loop forever.
            self._next_offset = None
        if added:
            self._set_status("")
        elif not self._items and self._next_offset is not None:
            # Empty first page but a next link: try once more automatically.
            self._load_page(self._next_offset)
        elif not self._items:
            self._set_status("No popular GIFs right now.", error=False)
        elif self._next_offset is None:
            self._set_status("That's all for now.")
        else:
            self._set_status("")
        if added:
            # If the page does not fill the viewport there is nothing to
            # scroll, so fetch the next offset automatically.
            GLib.timeout_add(120, self._maybe_fill_view)
        return False

    def _maybe_fill_view(self) -> bool:
        if not self._alive or self._loading or self._next_offset is None:
            return False
        try:
            adjustment = self.scroller.get_vadjustment()
            upper = adjustment.get_upper()
            if upper > 0 and upper <= adjustment.get_page_size() + 24:
                self._load_page(self._next_offset)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _retry(self) -> None:
        offset = self._failed_offset
        self._next_offset = offset if offset is not None else self._next_offset
        self._load_page(offset)

    def _on_scroll(self, adjustment) -> None:
        if self._loading or self._done or self._next_offset is None:
            return
        try:
            if adjustment.get_value() + adjustment.get_page_size() >= adjustment.get_upper() - self._NEAR_BOTTOM:
                self._load_page(self._next_offset)
        except Exception:  # noqa: BLE001
            pass

    # -- insertion --------------------------------------------------------- #
    def _choose(self, media: dict) -> None:
        try:
            self.on_insert(dict(media))
        except Exception as exc:  # noqa: BLE001
            self._set_status(_error_text(exc), error=True)
            return
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # -- lifecycle --------------------------------------------------------- #
    def _on_closed(self, *_args) -> None:
        self._alive = False
        try:
            if self._vadj_handler:
                self.scroller.get_vadjustment().disconnect(self._vadj_handler)
        except Exception:  # noqa: BLE001
            pass
        self._vadj_handler = 0


# --------------------------------------------------------------------------- #
# The tray itself
# --------------------------------------------------------------------------- #

class AttachTray(Gtk.Box):
    """Composer attachment toolbar.

    Exact API: ``AttachTray(on_insert)``.  ``on_insert(media_dict)`` is called
    once per uploaded image or picked GIF; the dict is already a Tumblr media
    object (the ``response`` envelope of ``upload_image`` is unwrapped).
    """

    __gtype_name__ = "DeskboardAttachTray"

    def __init__(self, on_insert: Callable[[dict], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        _install_css()
        self.on_insert = on_insert
        self._ctx: Any = None
        self._alive = True
        self._busy = False
        self._file_dialog: Gtk.FileDialog | None = None
        self._gif_dialog: _GifPickerDialog | None = None
        self._status_timer = 0
        self._pulse_id = 0
        self.add_css_class("db-attach-tray")

        self.image_button = Gtk.Button.new_from_icon_name("insert-image-symbolic")
        self.image_button.add_css_class("flat")
        self.image_button.set_tooltip_text("Add an image")
        self.image_button.set_valign(Gtk.Align.CENTER)
        self.image_button.connect("clicked", lambda *_a: self._on_add_image())
        self.append(self.image_button)

        self.gif_button = Gtk.Button(label="GIF")
        self.gif_button.add_css_class("flat")
        self.gif_button.set_tooltip_text("Add a popular GIF")
        self.gif_button.set_valign(Gtk.Align.CENTER)
        self.gif_button.connect("clicked", lambda *_a: self._open_gif_picker())
        self.append(self.gif_button)

        self.status_label = Gtk.Label(label="")
        self.status_label.add_css_class("db-attach-status")
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_max_width_chars(46)
        self.status_label.set_xalign(0)
        self.status_label.set_valign(Gtk.Align.CENTER)
        self.status_label.set_visible(False)
        self.append(self.status_label)

        self.progress = Gtk.ProgressBar()
        self.progress.set_size_request(64, -1)
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress.set_visible(False)
        self.append(self.progress)

    # -- context ----------------------------------------------------------- #
    def set_context(self, ctx: Any) -> None:
        """Optional explicit context; otherwise resolved from the root window."""
        self._ctx = ctx

    def _resolve_ctx(self) -> Any:
        if self._ctx is not None:
            return self._ctx
        try:
            root = self.get_root()
        except Exception:  # noqa: BLE001
            root = None
        candidates = [root]
        try:
            if root is not None:
                candidates.append(root.get_transient_for())
        except Exception:  # noqa: BLE001
            pass
        for candidate in candidates:
            if candidate is None:
                continue
            ctx = getattr(candidate, "ctx", None)
            if ctx is not None:
                self._ctx = ctx
                return ctx
        return None

    # -- status ------------------------------------------------------------ #
    def _set_status(self, text: str, error: bool = False, clear_after: int = 0) -> None:
        try:
            self.status_label.set_text(text)
            self.status_label.set_visible(bool(text))
            if error:
                self.status_label.add_css_class("db-attach-error")
            else:
                self.status_label.remove_css_class("db-attach-error")
        except Exception:  # noqa: BLE001
            pass
        if self._status_timer:
            try:
                GLib.source_remove(self._status_timer)
            except Exception:  # noqa: BLE001
                pass
            self._status_timer = 0
        if clear_after > 0:
            self._status_timer = GLib.timeout_add_seconds(clear_after, self._clear_status)

    def _clear_status(self) -> bool:
        self._status_timer = 0
        try:
            self.status_label.set_text("")
            self.status_label.set_visible(False)
            self.status_label.remove_css_class("db-attach-error")
        except Exception:  # noqa: BLE001
            pass
        return False

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        try:
            self.image_button.set_sensitive(not busy)
            self.gif_button.set_sensitive(not busy)
            self.progress.set_visible(busy)
        except Exception:  # noqa: BLE001
            pass
        if busy:
            if not self._pulse_id:
                self._pulse_id = GLib.timeout_add(120, self._pulse_progress)
        elif self._pulse_id:
            try:
                GLib.source_remove(self._pulse_id)
            except Exception:  # noqa: BLE001
                pass
            self._pulse_id = 0

    def _pulse_progress(self) -> bool:
        try:
            self.progress.pulse()
        except Exception:  # noqa: BLE001
            pass
        return bool(self._busy)

    # -- image upload ------------------------------------------------------ #
    def _on_add_image(self) -> None:
        if self._busy:
            return
        ctx = self._resolve_ctx()
        if ctx is None:
            self._set_status("Sign in to attach media.", error=True)
            return
        try:
            dialog = Gtk.FileDialog()
            dialog.set_title("Attach an image")
            image_filter = Gtk.FileFilter()
            image_filter.set_name("Images (PNG, JPEG, GIF, WebP)")
            for mime_type in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                image_filter.add_mime_type(mime_type)
            for pattern in ("*.png", "*.PNG", "*.jpg", "*.JPG", "*.jpeg", "*.JPEG",
                            "*.gif", "*.GIF", "*.webp", "*.WEBP"):
                image_filter.add_pattern(pattern)
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(image_filter)
            dialog.set_filters(filters)
            dialog.set_default_filter(image_filter)
            self._file_dialog = dialog
            parent = self.get_root()
            if not isinstance(parent, Gtk.Window):
                parent = None
            dialog.open(parent, None, self._on_file_chosen)
        except Exception:  # noqa: BLE001
            self._set_status("Could not open the file picker.", error=True)

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result: Any) -> None:
        self._file_dialog = None
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # dismissed by the user
        except Exception:  # noqa: BLE001
            self._set_status("Could not read that file.", error=True)
            return
        if file is None:
            return
        try:
            path = file.get_path()
        except Exception:  # noqa: BLE001
            path = None
        if not path:
            self._set_status("That file is not available locally.", error=True)
            return
        self._start_upload(path)

    def _start_upload(self, path: str) -> None:
        """Validate and upload ``path`` on a worker thread (explicit action only)."""
        if self._busy:
            return
        ctx = self._resolve_ctx()
        if ctx is None:
            self._set_status("Sign in to attach media.", error=True)
            return
        if getattr(ctx, "demo", False):
            self._set_status("Image upload is unavailable in demo mode.", error=True)
            return
        if not hasattr(getattr(ctx, "client", None), "upload_image"):
            self._set_status("This build cannot upload media.", error=True)
            return
        local = Path(path).expanduser()
        try:
            if not local.is_file():
                self._set_status("Could not read that file.", error=True)
                return
            size = local.stat().st_size
        except OSError:
            self._set_status("Could not read that file.", error=True)
            return
        extension = local.suffix.lower()
        mime = (mimetypes.guess_type(str(local))[0] or "").lower()
        if extension not in SUPPORTED_EXTENSIONS or not mime.startswith("image/"):
            self._set_status("Unsupported image type. Use PNG, JPEG, GIF or WebP.", error=True)
            return
        if size > MAX_UPLOAD_BYTES:
            self._set_status(
                f"That image is {_nice_size(size)}; the limit is "
                f"{_nice_size(MAX_UPLOAD_BYTES)}.",
                error=True,
            )
            return
        self._set_busy(True)
        self._set_status("Uploading…")

        def work() -> None:
            try:
                result = ctx.client.upload_image(str(local))
            except BaseException as exc:  # noqa: BLE001 - reported inline
                GLib.idle_add(self._upload_finished, False, exc)
                return
            GLib.idle_add(self._upload_finished, True, result)

        threading.Thread(target=work, daemon=True, name="media-upload").start()

    def _upload_finished(self, ok: bool, payload: Any) -> bool:
        if not self._alive:
            return False
        try:
            if self.get_root() is None:
                return False
        except Exception:  # noqa: BLE001
            pass
        try:
            self._set_busy(False)
        except Exception:  # noqa: BLE001
            pass
        if not ok:
            self._set_status(_error_text(payload), error=True)
            return False
        media = _media_object(payload)
        if not media:
            self._set_status("Tumblr did not return an image for that upload.", error=True)
            return False
        try:
            self.on_insert(media)
        except Exception as exc:  # noqa: BLE001
            self._set_status(_error_text(exc), error=True)
            return False
        self._set_status("Image attached.", clear_after=4)
        return False

    # -- GIF picker -------------------------------------------------------- #
    def _open_gif_picker(self) -> None:
        ctx = self._resolve_ctx()
        if ctx is None:
            self._set_status("Sign in to browse GIFs.", error=True)
            return
        if getattr(ctx, "demo", False):
            self._set_status("The GIF picker is unavailable in demo mode.", error=True)
            return
        if not hasattr(getattr(ctx, "client", None), "get_gif_popular"):
            self._set_status("This build cannot browse GIFs.", error=True)
            return
        parent = self.get_root()
        if not isinstance(parent, Gtk.Window):
            parent = None
        try:
            dialog = _GifPickerDialog(ctx, self._on_gif_picked, parent)
            self._gif_dialog = dialog
        except Exception:  # noqa: BLE001
            self._set_status("Could not open the GIF picker.", error=True)

    def _on_gif_picked(self, media: dict) -> None:
        self.on_insert(media)
        self._set_status("GIF attached.", clear_after=4)

    # -- Gtk lifecycle ----------------------------------------------------- #
    def do_unroot(self) -> None:
        self._alive = False
        if self._pulse_id:
            try:
                GLib.source_remove(self._pulse_id)
            except Exception:  # noqa: BLE001
                pass
            self._pulse_id = 0
        dialog, self._gif_dialog = self._gif_dialog, None
        if dialog is not None:
            try:
                dialog.close()
            except Exception:  # noqa: BLE001
                pass
        Gtk.Widget.do_unroot(self)
