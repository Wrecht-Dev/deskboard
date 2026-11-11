"""Media widgets: photo grids, GIF playback, inline video and audio.

Owner: media.  Public surface (see ``docs/INTERFACES.md``):

    ImageGrid(medias, ctx, colors=None, post=None)
    VideoBlock(block, ctx, post=None)
    AudioBlock(block, ctx, post=None)

Design notes
------------
* Photos use ``ctx.images`` exclusively; no decoding happens on the main loop.
* Every tile reserves its final height from ``media.width/height`` *before* the
  image arrives, so the feed never jumps when textures land.
* Animated GIFs are transcoded once (content-addressed) to a silent looping
  H.264 file and played with ``Gtk.Video``; the static GIF frame is the poster
  and the fallback when ffmpeg is missing.
* Videos and GIFs own their ``Gtk.MediaFile``.  ``do_unmap``/``do_unroot`` tear
  the stream down so a recycled row can never keep playing (or keep a decoder
  alive) after it scrolls away.
* Audio uses one shared player so playback survives feed scrolling; the stream
  is stopped when the track ends, another track starts, or no widget has
  reattached for a short grace period after its owning widget was destroyed.
"""
from __future__ import annotations

import weakref
from typing import Any, Callable, Iterable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Gio, GLib, Graphene, Gsk, Gtk, Pango

from .. import config
from ..models import Media, upscale_image_url
from ..services import media as mediasvc
from . import media_style

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

GAP = 6
_SINGLE_MAX_HEIGHT = 640
_SINGLE_MIN_HEIGHT = 120

_CSS_COLOR_CACHE: dict[str, Gtk.CssProvider] = {}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _format_seconds(seconds: float) -> str:
    total = int(max(0.0, float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_us(value: Any) -> str:
    try:
        return _format_seconds(float(value or 0) / 1_000_000)
    except (TypeError, ValueError):
        return "0:00"


def _aspect_of(media: Media) -> float:
    try:
        if media.width > 0 and media.height > 0:
            return _clamp(media.width / media.height, 0.2, 6.0)
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def _icon(name: str, size: int = 16) -> Gtk.Image:
    image = Gtk.Image.new_from_icon_name(name)
    try:
        image.set_pixel_size(size)
    except Exception:  # noqa: BLE001
        pass
    return image


def _dim_label(text: str, css: tuple[str, ...] = ("db-media-error",)) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    for name in css:
        label.add_css_class(name)
    return label


def _normalise_colors(colors: Any) -> Any:
    return colors if isinstance(colors, dict) else None


def _paint_placeholder(widget: Gtk.Widget, colors: Any) -> None:
    """Give ``widget`` the post's palette as its placeholder colour."""
    colour = mediasvc.placeholder_color(_normalise_colors(colors))
    if not colour:
        return
    provider = _CSS_COLOR_CACHE.get(colour)
    if provider is None:
        if len(_CSS_COLOR_CACHE) > 256:
            _CSS_COLOR_CACHE.clear()
        css = f".db-ph-{colour} {{ background-color: #{colour}; }}"
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(css.encode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        _CSS_COLOR_CACHE[colour] = provider
    try:
        widget.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        widget.add_css_class(f"db-ph-{colour}")
    except Exception:  # noqa: BLE001
        pass


def _make_clickable(widget: Gtk.Widget, callback: Callable[[int], None]) -> None:
    gesture = Gtk.GestureClick()
    gesture.set_button(1)
    widget.add_controller(gesture)

    def released(gesture_, n_press, x, y):
        try:
            if gesture_.get_current_button() == 1:
                callback(n_press)
        except Exception:  # noqa: BLE001 - widget may be gone
            pass

    gesture.connect("released", released)
    try:
        widget.set_cursor_from_name("pointer")
    except Exception:  # noqa: BLE001
        pass


def _safe_pref(ctx: Any, key: str, default: bool = False) -> bool:
    try:
        value = ctx.prefs.get(key)
        return bool(default if value is None else value)
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------- #
# Application-shutdown cleanup
# --------------------------------------------------------------------------- #
# GTK 4 does not call ``do_unmap``/``do_unroot`` while a toplevel is destroyed
# by ``Gtk.Application`` shutdown, and tearing down a playing GtkVideo at that
# point can crash inside GStreamer/GL.  Active players therefore register a
# weak cleanup callback with the application's ``shutdown`` signal so streams
# are stopped while the widget tree is still intact.  Normal recycling still
# uses do_unmap/do_unroot.
_ACTIVE_CLEANUPS: list[weakref.WeakMethod] = []
_SHUTDOWN_HOOKED = False


def _ensure_shutdown_hook() -> None:
    global _SHUTDOWN_HOOKED
    if _SHUTDOWN_HOOKED:
        return
    _SHUTDOWN_HOOKED = True
    try:
        application = Gtk.Application.get_default()
        if application is not None:
            application.connect("shutdown", _on_application_shutdown)
    except Exception:  # noqa: BLE001 - tests without an application
        pass


def _register_media_cleanup(callback: Callable[[], None]) -> None:
    _ensure_shutdown_hook()
    try:
        _ACTIVE_CLEANUPS.append(weakref.WeakMethod(callback))
    except TypeError:  # pragma: no cover - exotic callables
        pass


def _unregister_media_cleanup(callback: Callable[[], None]) -> None:
    for reference in list(_ACTIVE_CLEANUPS):
        current = reference()
        if current is None or current == callback:
            try:
                _ACTIVE_CLEANUPS.remove(reference)
            except ValueError:
                pass


def _on_application_shutdown(_application) -> None:
    for reference in list(_ACTIVE_CLEANUPS):
        callback = reference()
        if callback is None:
            continue
        try:
            callback()
        except Exception:  # noqa: BLE001 - never block shutdown
            pass
    _ACTIVE_CLEANUPS.clear()
    session = _AudioSession._instance
    if session is not None:
        try:
            session.stop()
        except Exception:  # noqa: BLE001
            pass


class _Frame(Gtk.Overlay):
    """A clipped, rounded rectangle used for every media surface.

    A Gtk.Overlay (rather than a Gtk.Box) so its single child always fills the
    frame, while the frame keeps its own aspect-derived height.
    """

    __gtype_name__ = "DeskboardMediaFrame"

    def __init__(self, css_class: str = "db-media-frame") -> None:
        super().__init__()
        self.add_css_class(css_class)
        self.set_hexpand(True)
        self.set_vexpand(False)
        try:
            self.set_overflow(Gtk.Overflow.HIDDEN)
        except Exception:  # noqa: BLE001 - GTK < 4.8
            pass


class _MediaBox(Gtk.Widget):
    """A clipped rounded surface with an exact, aspect-derived height.

    ``Gtk.Picture``/``Gtk.Video`` report their intrinsic media size as the
    natural height, which stretches a card when it is the only child of a
    vertical box.  This container reports *only* the configured height and
    allocates its child to fill it, so video cards keep the poster's aspect.
    """

    __gtype_name__ = "DeskboardMediaBox"

    def __init__(self, css_class: str = "db-media-frame") -> None:
        super().__init__()
        self.add_css_class(css_class)
        self.set_hexpand(True)
        self.set_vexpand(False)
        self._child: Optional[Gtk.Widget] = None
        self._height = 200
        self._width: Optional[int] = None
        try:
            self.set_overflow(Gtk.Overflow.HIDDEN)
        except Exception:  # noqa: BLE001 - GTK < 4.8
            pass

    def set_child(self, child: Optional[Gtk.Widget]) -> None:
        if self._child is not None:
            self._child.unparent()
        self._child = child
        if child is not None:
            child.set_parent(self)

    def get_child(self) -> Optional[Gtk.Widget]:
        return self._child

    def set_media_height(self, height: int) -> None:
        self._height = max(1, int(height))
        self.queue_resize()

    def set_media_size(self, width: int, height: int) -> None:
        self._width = max(1, int(width))
        self._height = max(1, int(height))
        self.queue_resize()

    def do_measure(self, orientation, for_size):  # noqa: N802 - vfunc
        if orientation == Gtk.Orientation.HORIZONTAL:
            if self._width is not None:
                return (self._width, self._width, -1, -1)
            return (0, 1, -1, -1)
        return (self._height, self._height, -1, -1)

    def do_size_allocate(self, width, height, baseline):  # noqa: N802 - vfunc
        if self._child is not None:
            self._child.allocate(width, height, baseline, None)

    def do_dispose(self) -> None:  # noqa: N802 - vfunc
        child, self._child = self._child, None
        if child is not None:
            child.unparent()
        Gtk.Widget.do_dispose(self)


# --------------------------------------------------------------------------- #
# Seek bar shared by audio and video
# --------------------------------------------------------------------------- #

class _SeekScale(Gtk.Scale):
    """Tiny range wrapper that does not fight programmatic position updates."""

    __gtype_name__ = "DeskboardSeekScale"

    def __init__(self, on_seek: Callable[[int], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self._on_seek = on_seek
        self._updating = False
        self._can_seek = False
        self.set_hexpand(True)
        self.set_size_request(120, -1)
        self.set_draw_value(False)
        self.set_inverted(False)
        # Keep the scale sensitive so the trough is visible before playback;
        # seeks are gated on the stream actually having a duration.
        self.set_sensitive(True)
        self.get_adjustment().set_page_increment(0)
        self.get_adjustment().connect("value-changed", self._value_changed)

    def _value_changed(self, adjustment) -> None:
        if self._updating or not self._can_seek:
            return
        try:
            self._on_seek(int(adjustment.get_value()))
        except Exception:  # noqa: BLE001
            pass

    def set_duration(self, microseconds: int) -> None:
        duration = max(1, int(microseconds or 0))
        self._updating = True
        try:
            adjustment = self.get_adjustment()
            adjustment.set_upper(duration)
            adjustment.set_value(min(adjustment.get_value(), duration))
        finally:
            self._updating = False
        self._can_seek = microseconds > 0

    def set_position(self, microseconds: int) -> None:
        if not self._can_seek:
            return
        self._updating = True
        try:
            self.get_adjustment().set_value(max(0, min(int(microseconds), int(self.get_adjustment().get_upper()))))
        finally:
            self._updating = False


# --------------------------------------------------------------------------- #
# GIF playback
# --------------------------------------------------------------------------- #

class _GifContent(Gtk.Overlay):
    """Animated GIF rendered as a looping, silent MP4 with a static poster.

    One ``Gtk.MediaFile`` exists only while the widget is mapped; it is torn
    down in :meth:`do_unmap`/[:meth:`do_unroot`].
    """

    __gtype_name__ = "DeskboardGifContent"

    def __init__(self, media: Media, ctx: Any, width_hint: int, colors: Any = None) -> None:
        super().__init__()
        self.media = media
        self.ctx = ctx
        self._width_hint = width_hint
        self._alive = True
        self._mapped = False
        self._requested = False
        self._path = None
        self._stream = None
        self._poster_failed = False
        self._transcode_done = False
        self.set_hexpand(True)
        self.set_vexpand(False)
        self.set_overflow(Gtk.Overflow.HIDDEN)

        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(False)
        self.set_child(self.stack)

        self.poster = Gtk.Picture()
        self.poster.set_content_fit(Gtk.ContentFit.COVER)
        self.poster.set_can_shrink(True)
        self.poster.set_hexpand(True)
        self.poster.set_vexpand(False)
        self.stack.add_named(self.poster, "poster")

        self.video = Gtk.Video()
        self.video.set_hexpand(True)
        self.video.set_vexpand(False)
        try:
            self.video.set_property("graphics-offload", False)
        except Exception:  # noqa: BLE001
            pass
        self.stack.add_named(self.video, "video")

        error_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        error_box.set_halign(Gtk.Align.CENTER)
        error_box.set_valign(Gtk.Align.CENTER)
        error_box.append(_icon("image-missing-symbolic", 20))
        error_box.append(_dim_label("GIF unavailable"))
        self.stack.add_named(error_box, "error")
        self.stack.set_visible_child_name("poster")

        _register_media_cleanup(self._shutdown)
        self._load_poster()

    def _shutdown(self) -> None:
        self._teardown()

    # -- poster ------------------------------------------------------------ #
    def _load_poster(self) -> None:
        url = self.media.url
        if not url:
            self._poster_failed = True
            return
        target_width = max(int(self._width_hint), 500)

        def apply(texture) -> None:
            if not self._alive:
                return
            if texture is None:
                self._poster_failed = True
                if self._transcode_done and not self._path:
                    self.stack.set_visible_child_name("error")
            else:
                self.poster.set_paintable(texture)

        try:
            self.ctx.images.load(upscale_image_url(url, 500), target_width, target_width, apply)
        except Exception:  # noqa: BLE001
            self._poster_failed = True

    # -- play/teardown ----------------------------------------------------- #
    def _request_transcode(self) -> None:
        if self._requested:
            return
        self._requested = True

        def done(path) -> None:
            if not self._alive:
                return
            self._transcode_done = True
            self._path = path
            if path:
                if self._mapped:
                    self._play()
            elif self._poster_failed:
                self.stack.set_visible_child_name("error")

        mediasvc.gif_to_mp4_async(self.media.url, done)

    def _play(self) -> None:
        if not self._alive or not self._mapped or not self._path:
            return
        try:
            if self._stream is None:
                self._stream = Gtk.MediaFile.new_for_filename(str(self._path))
                # Explicit restart on end rather than GtkMediaStream's loop
                # property: GStreamer's loop flag behaves inconsistently across
                # sinks (it can freeze the last frame while reporting playing).
                self._stream.set_loop(False)
                self._stream.set_muted(True)
                self._stream.set_volume(0.0)
                self._stream.connect("notify::error", self._on_stream_error)
                self._stream.connect("notify::ended", self._on_stream_ended)
                self.video.set_media_stream(self._stream)
            self.stack.set_visible_child_name("video")
            self._stream.set_playing(True)
        except Exception:  # noqa: BLE001 - treat as a static poster
            self._teardown()

    def _teardown(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.set_playing(False)
                stream.pause()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.video.set_media_stream(None)
        except Exception:  # noqa: BLE001
            pass
        if self.stack.get_visible_child_name() == "video":
            self.stack.set_visible_child_name("error" if self._poster_failed else "poster")

    def _on_stream_error(self, stream, _pspec) -> None:
        if not self._alive:
            return
        self._teardown()
        if self._poster_failed:
            self.stack.set_visible_child_name("error")

    def _on_stream_ended(self, stream, _pspec) -> None:
        if not self._alive or not self._mapped or self._path is None or stream is not self._stream:
            return
        try:
            stream.seek(0)
            stream.set_playing(True)
        except Exception:  # noqa: BLE001 - keep the poster instead
            pass

    # -- Gtk lifecycle ----------------------------------------------------- #
    def do_map(self) -> None:
        Gtk.Widget.do_map(self)
        self._mapped = True
        self._request_transcode()
        if self._path:
            self._play()

    def do_unmap(self) -> None:
        self._mapped = False
        self._teardown()
        Gtk.Widget.do_unmap(self)

    def do_unroot(self) -> None:
        self._alive = False
        self._mapped = False
        self._teardown()
        _unregister_media_cleanup(self._shutdown)
        Gtk.Widget.do_unroot(self)


# --------------------------------------------------------------------------- #
# A single image tile
# --------------------------------------------------------------------------- #

class _MediaTile(Gtk.Overlay):
    """One rounded photo cell with placeholder, GIF badge and failure state."""

    __gtype_name__ = "DeskboardMediaTile"

    def __init__(self, media: Media, ctx: Any, index: int, width_hint: int,
                 colors: Any, on_click: Callable[[int], None],
                 extra_overlay: Gtk.Widget | None = None,
                 show_spinner: bool = False, single: bool = False) -> None:
        super().__init__()
        self.media = media
        self.ctx = ctx
        self.index = index
        self._alive = True
        self._loaded = False
        self._single = single
        self._width_hint = width_hint
        self.set_hexpand(True)
        self.set_vexpand(False)

        self.frame = _Frame()
        _paint_placeholder(self.frame, colors)
        self.set_child(self.frame)

        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(False)
        self.frame.set_child(self.stack)

        self.is_gif = mediasvc.media_is_gif(media)
        if self.is_gif:
            self.content: Gtk.Widget = _GifContent(media, ctx, width_hint, colors)
        else:
            picture = Gtk.Picture()
            picture.set_content_fit(Gtk.ContentFit.COVER)
            picture.set_can_shrink(True)
            picture.set_hexpand(True)
            picture.set_vexpand(False)
            picture.set_size_request(1, 1)
            self.picture = picture
            self.content = picture
        self.stack.add_named(self.content, "content")

        error_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        error_box.set_halign(Gtk.Align.CENTER)
        error_box.set_valign(Gtk.Align.CENTER)
        error_box.append(_icon("image-missing-symbolic", 22))
        error_box.append(_dim_label("Image unavailable"))
        self.stack.add_named(error_box, "error")
        self.stack.set_visible_child_name("content")

        # NOTE: no Gtk.Spinner here.  The shared ImageCache delivers textures
        # through a default-idle GLib source, and a continuously animating
        # spinner keeps the frame clock busy at a higher priority, starving that
        # source (the image then never arrives).  A static placeholder colour is
        # shown instead, which also keeps the feed from jumping.
        if self.is_gif:
            self._badge("GIF", halign=Gtk.Align.START)
        if extra_overlay is not None:
            self.add_overlay(extra_overlay)

        _make_clickable(self, lambda _n: on_click(index))
        if not self.is_gif:
            self._load()

    # -- loading ----------------------------------------------------------- #
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        url = upscale_image_url(self.media.url, 1280 if self._single else 800)
        width = max(320, int(self._width_hint))
        height = max(320, int(self._width_hint * 1.5))

        def apply(texture) -> None:
            if not self._alive:
                return
            self._stop_spinner()
            if texture is None:
                self.stack.set_visible_child_name("error")
                return
            try:
                self.picture.set_paintable(texture)
                self.stack.set_visible_child_name("content")
            except Exception:  # noqa: BLE001 - recycled widget
                pass

        try:
            self.ctx.images.load(url, width, height, apply)
        except Exception:  # noqa: BLE001
            self._stop_spinner()
            self.stack.set_visible_child_name("error")

    def _stop_spinner(self) -> None:
        # Kept as a no-op hook for call sites that used to stop a spinner.
        return

    # -- overlays ---------------------------------------------------------- #
    def _badge(self, text: str, halign: Gtk.Align = Gtk.Align.END,
               valign: Gtk.Align = Gtk.Align.START) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.add_css_class("db-media-badge")
        label.set_halign(halign)
        label.set_valign(valign)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_can_target(False)
        self.add_overlay(label)
        return label

    def _show_error(self) -> None:
        try:
            self.stack.set_visible_child_name("error")
        except Exception:  # noqa: BLE001
            pass

    # -- Gtk lifecycle ----------------------------------------------------- #
    def do_unroot(self) -> None:
        self._alive = False
        Gtk.Widget.do_unroot(self)


def _count_overlay(extra: int) -> Gtk.Widget:
    overlay = Gtk.Overlay()
    overlay.set_hexpand(True)
    overlay.set_vexpand(True)
    scrim = Gtk.Box()
    scrim.add_css_class("db-media-scrim")
    scrim.set_hexpand(True)
    scrim.set_vexpand(True)
    scrim.set_can_target(False)
    overlay.set_child(scrim)
    label = Gtk.Label(label=f"+{extra}")
    label.add_css_class("db-media-pill")
    label.set_halign(Gtk.Align.CENTER)
    label.set_valign(Gtk.Align.CENTER)
    label.set_can_target(False)
    overlay.add_overlay(label)
    return overlay


# --------------------------------------------------------------------------- #
# ImageGrid
# --------------------------------------------------------------------------- #

class ImageGrid(Gtk.Widget):
    """Composed photo set: 1 hero, 2 columns, 3 (big + stack) or a 2x2 grid.

    A custom layout container: GTK 4 does not notify widgets about allocation
    width changes (``notify::width`` never fires), so the grid computes cell
    rectangles from the width it is actually allocated and requests a corrected
    height on the next layout pass.
    """

    __gtype_name__ = "DeskboardImageGrid"

    def __init__(self, medias: Iterable[Media], ctx: Any, colors: Any = None,
                 post: Any = None) -> None:
        super().__init__()
        media_style.install()
        self.ctx = ctx
        self.post = post
        self.colors = _normalise_colors(colors)
        self.medias = _dedupe_medias(medias)
        self._tiles: list[_MediaTile] = []
        self._width_hint = max(1, config.CONTENT_MAX_WIDTH)
        self.set_hexpand(True)
        self.set_vexpand(False)
        if not self.medias:
            return

        count = len(self.medias)
        visible = min(count, 4)
        for index in range(visible):
            extra = None
            if count > 4 and index == 3:
                extra = _count_overlay(count - 4)
            tile = _MediaTile(
                self.medias[index],
                ctx,
                index if not (count > 4 and index == 3) else 4,
                1280 if count == 1 else 800,
                self.colors,
                self._open,
                extra_overlay=extra,
                show_spinner=(count == 1),
                single=(count == 1),
            )
            tile.set_parent(self)
            self._tiles.append(tile)
        self.set_size_request(-1, self._total_height(self._width_hint))

    # -- geometry ---------------------------------------------------------- #
    def _rects(self, width: int) -> list[tuple[Gtk.Widget, int, int, int, int]]:
        width = max(1, int(width))
        count = len(self.medias)
        if count == 0 or not self._tiles:
            return []
        if count == 1:
            aspect = _clamp(_aspect_of(self.medias[0]), 0.3, 4.0)
            height = int(_clamp(width / aspect, _SINGLE_MIN_HEIGHT, _SINGLE_MAX_HEIGHT))
            return [(self._tiles[0], 0, 0, width, height)]
        if count == 2:
            cell = max(1, (width - GAP) // 2)
            aspect = _clamp(
                sum(_aspect_of(media) for media in self.medias[:2]) / 2, 0.5, 2.2
            )
            height = int(_clamp(cell / aspect, 140, 480))
            return [
                (self._tiles[0], 0, 0, cell, height),
                (self._tiles[1], cell + GAP, 0, width - cell - GAP, height),
            ]
        if count == 3:
            cell = max(1, (width - 2 * GAP) // 3)
            left = cell * 2 + GAP
            aspect = _clamp(_aspect_of(self.medias[0]), 0.4, 3.0)
            height = int(_clamp(left / aspect, 240, 600))
            stacked = max(1, (height - GAP) // 2)
            right = max(1, width - left - GAP)
            return [
                (self._tiles[0], 0, 0, left, height),
                (self._tiles[1], left + GAP, 0, right, stacked),
                (self._tiles[2], left + GAP, stacked + GAP, right, stacked),
            ]
        cell = max(1, (width - GAP) // 2)
        aspects = [_aspect_of(media) for media in self.medias[:4]]
        aspect = _clamp(sum(aspects) / len(aspects), 0.5, 2.4)
        height = int(_clamp(cell / aspect, 150, 420))
        return [
            (self._tiles[0], 0, 0, cell, height),
            (self._tiles[1], cell + GAP, 0, width - cell - GAP, height),
            (self._tiles[2], 0, height + GAP, cell, height),
            (self._tiles[3], cell + GAP, height + GAP, width - cell - GAP, height),
        ]

    def _total_height(self, width: int) -> int:
        rects = self._rects(width)
        if not rects:
            return 0
        return max(y + height for _tile, _x, y, _w, height in rects)

    # -- Gtk layout -------------------------------------------------------- #
    def do_measure(self, orientation, for_size):  # noqa: N802 - vfunc
        if orientation == Gtk.Orientation.HORIZONTAL:
            return (1, max(1, config.CONTENT_MAX_WIDTH), -1, -1)
        height = self._total_height(self._width_hint)
        return (height, height, -1, -1)

    def do_size_allocate(self, width, height, baseline):  # noqa: N802 - vfunc
        width = max(1, int(width))
        if abs(width - self._width_hint) > 2:
            self._width_hint = width
            self.set_size_request(-1, self._total_height(width))
            self.queue_resize()
        for tile, x, y, cell_width, cell_height in self._rects(width):
            transform = Gsk.Transform().translate(Graphene.Point().init(x, y))
            tile.allocate(cell_width, cell_height, baseline, transform)

    def do_dispose(self) -> None:  # noqa: N802 - vfunc
        tiles, self._tiles = self._tiles, []
        for tile in tiles:
            tile.unparent()
        Gtk.Widget.do_dispose(self)

    # -- interaction ------------------------------------------------------- #
    def _open(self, index: int) -> None:
        from .viewer import open_viewer

        open_viewer(self.ctx, self.medias, index=index, post=self.post)


def _dedupe_medias(medias: Iterable[Media]) -> list[Media]:
    out: list[Media] = []
    seen: set[str] = set()
    for media in medias or []:
        url = str(getattr(media, "url", "") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(media)
    return out


# --------------------------------------------------------------------------- #
# Inline video player
# --------------------------------------------------------------------------- #

class _InlineVideo(Gtk.Overlay):
    """``Gtk.Video`` + controls that owns exactly one ``Gtk.MediaFile``."""

    __gtype_name__ = "DeskboardInlineVideo"

    def __init__(self, url: str, duration_hint: float = 0.0, muted: bool = False,
                 on_error: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._url = url
        self._duration_hint = duration_hint
        self._muted = muted
        self._on_error = on_error
        self._stream = None
        self._ticker = 0
        self._alive = True
        self._started = False
        self.set_hexpand(True)
        self.set_vexpand(True)

        self.video = Gtk.Video()
        self.video.set_hexpand(True)
        self.video.set_vexpand(False)
        try:
            self.video.set_property("graphics-offload", False)
        except Exception:  # noqa: BLE001
            pass
        self.set_child(self.video)

        self.loading_label = Gtk.Label(label="Loading video…")
        self.loading_label.add_css_class("db-media-badge")
        self.loading_label.set_halign(Gtk.Align.CENTER)
        self.loading_label.set_valign(Gtk.Align.CENTER)
        self.loading_label.set_can_target(False)
        self.add_overlay(self.loading_label)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.add_css_class("db-video-controls")
        controls.set_halign(Gtk.Align.FILL)
        controls.set_valign(Gtk.Align.END)
        controls.set_margin_start(8)
        controls.set_margin_end(8)
        controls.set_margin_bottom(8)

        self.play_button = Gtk.Button()
        self.play_button.add_css_class("flat")
        self.play_button.set_child(_icon("media-playback-pause-symbolic", 18))
        self.play_button.set_tooltip_text("Play / pause")
        self.play_button.connect("clicked", self._on_play_clicked)
        controls.append(self.play_button)

        self.seek = _SeekScale(self._seek)
        controls.append(self.seek)

        self.time_label = _dim_label("0:00", css=("db-video-time", "dim-label"))
        self.time_label.set_label("0:00")
        controls.append(self.time_label)

        self.mute_button = Gtk.Button()
        self.mute_button.add_css_class("flat")
        self.mute_button.set_child(_icon(
            "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic", 16
        ))
        self.mute_button.set_tooltip_text("Mute / unmute")
        self.mute_button.connect("clicked", self._on_mute_clicked)
        controls.append(self.mute_button)
        self.add_overlay(controls)

        _register_media_cleanup(self.stop_playback)
        self.start(muted=muted)

    # -- stream ------------------------------------------------------------ #
    def _create_stream(self) -> bool:
        try:
            stream = Gtk.MediaFile.new_for_file(Gio.File.new_for_uri(self._url))
        except Exception:  # noqa: BLE001
            self._failed()
            return False
        try:
            stream.set_loop(False)
            stream.set_muted(self._muted)
            stream.set_volume(0.0 if self._muted else 1.0)
        except Exception:  # noqa: BLE001
            pass
        stream.connect("notify::prepared", self._on_prepared)
        stream.connect("notify::error", self._on_stream_error)
        stream.connect("notify::ended", self._on_ended)
        stream.connect("notify::duration", self._on_duration_changed)
        self._stream = stream
        try:
            self.video.set_media_stream(stream)
        except Exception:  # noqa: BLE001
            pass
        return True

    def start(self, muted: Optional[bool] = None) -> None:
        if muted is not None:
            self._muted = bool(muted)
        if self._stream is None and not self._create_stream():
            return
        self._started = True
        try:
            self._stream.set_muted(self._muted)
            self._stream.set_playing(True)
        except Exception:  # noqa: BLE001
            self._failed()
            return
        self.loading_label.set_visible(True)
        self._sync_play_icon()
        self._start_ticker()

    def stop_playback(self) -> None:
        """Pause and destroy the decoder; safe to call repeatedly."""
        self._stop_ticker()
        stream, self._stream = self._stream, None
        if stream is not None:
            for handler in ("notify::prepared", "notify::error", "notify::ended",
                            "notify::duration"):
                try:
                    stream.disconnect_by_func(getattr(self, {
                        "notify::prepared": "_on_prepared",
                        "notify::error": "_on_stream_error",
                        "notify::ended": "_on_ended",
                        "notify::duration": "_on_duration_changed",
                    }[handler]))
                except Exception:  # noqa: BLE001
                    pass
            try:
                stream.set_playing(False)
                stream.pause()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.video.set_media_stream(None)
        except Exception:  # noqa: BLE001
            pass
        self.loading_label.set_visible(False)
        self.seek.set_duration(0)
        self._sync_play_icon()

    def resume(self, muted: Optional[bool] = None) -> None:
        if muted is not None:
            self._muted = bool(muted)
        if self._stream is None:
            self.start(muted=self._muted)
        else:
            try:
                self._stream.set_playing(True)
            except Exception:  # noqa: BLE001
                pass
            self._start_ticker()
            self._sync_play_icon()

    # -- ticker ------------------------------------------------------------ #
    def _start_ticker(self) -> None:
        if self._ticker:
            return
        self._ticker = GLib.timeout_add(250, self._tick)

    def _stop_ticker(self) -> None:
        if self._ticker:
            try:
                GLib.source_remove(self._ticker)
            except Exception:  # noqa: BLE001
                pass
            self._ticker = 0

    def _tick(self) -> bool:
        if not self._alive or self._stream is None:
            self._ticker = 0
            return False
        try:
            position = int(self._stream.get_timestamp() or 0)
            duration = int(self._stream.get_duration() or 0)
            self.seek.set_position(position)
            self.time_label.set_label(
                f"{_format_us(position)} / {_format_us(duration)}"
            )
            self._sync_play_icon()
        except Exception:  # noqa: BLE001
            self._ticker = 0
            return False
        return True

    # -- stream signals ---------------------------------------------------- #
    def _on_prepared(self, stream, _pspec) -> None:
        if not self._alive:
            return
        if stream.get_error():
            self._failed()
            return
        self.loading_label.set_visible(False)
        duration = int(stream.get_duration() or 0)
        self.seek.set_duration(duration)
        if duration <= 0 and self._duration_hint:
            self.seek.set_duration(int(self._duration_hint * 1_000_000))

    def _on_duration_changed(self, stream, _pspec) -> None:
        try:
            duration = int(stream.get_duration() or 0)
            if duration > 0:
                self.seek.set_duration(duration)
        except Exception:  # noqa: BLE001
            pass

    def _on_ended(self, stream, _pspec) -> None:
        self._stop_ticker()
        self._sync_play_icon()

    def _on_stream_error(self, stream, _pspec) -> None:
        if not self._alive:
            return
        self._failed()

    def _failed(self) -> None:
        self._stop_ticker()
        self.loading_label.set_visible(False)
        self.seek.set_duration(0)
        self.time_label.set_label("Unavailable")
        if self._on_error is not None:
            try:
                self._on_error()
            except Exception:  # noqa: BLE001
                pass

    # -- controls ---------------------------------------------------------- #
    def _on_play_clicked(self, _button) -> None:
        playing = False
        if self._stream is not None:
            try:
                playing = bool(self._stream.get_playing())
            except Exception:  # noqa: BLE001
                playing = False
        if playing:
            try:
                self._stream.set_playing(False)
            except Exception:  # noqa: BLE001
                pass
            self._sync_play_icon()
            return
        self.resume()

    def _on_mute_clicked(self, _button) -> None:
        self._muted = not self._muted
        if self._stream is not None:
            try:
                self._stream.set_muted(self._muted)
                self._stream.set_volume(0.0 if self._muted else 1.0)
            except Exception:  # noqa: BLE001
                pass
        self.mute_button.set_child(_icon(
            "audio-volume-muted-symbolic" if self._muted else "audio-volume-high-symbolic",
            16,
        ))

    def _seek(self, microseconds: int) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(microseconds)
        except Exception:  # noqa: BLE001
            pass

    def _sync_play_icon(self) -> None:
        playing = False
        if self._stream is not None:
            try:
                playing = bool(self._stream.get_playing())
            except Exception:  # noqa: BLE001
                playing = False
        icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        try:
            self.play_button.set_child(_icon(icon, 18))
        except Exception:  # noqa: BLE001
            pass

    # -- Gtk lifecycle ----------------------------------------------------- #
    def do_unmap(self) -> None:
        self.stop_playback()
        Gtk.Widget.do_unmap(self)

    def do_unroot(self) -> None:
        self._alive = False
        self.stop_playback()
        _unregister_media_cleanup(self.stop_playback)
        Gtk.Widget.do_unroot(self)


# --------------------------------------------------------------------------- #
# VideoBlock
# --------------------------------------------------------------------------- #

_PROVIDER_LABELS = {
    "youtube": "YouTube",
    "vimeo": "Vimeo",
    "tumblr": "Tumblr",
}


class VideoBlock(Gtk.Widget):
    """Poster + play affordance; Tumblr-hosted video plays inline on click.

    Custom widget rather than a Gtk.Box: the poster/intrinsic video size would
    otherwise become the natural height and stretch the card.  It reports a
    height derived from its allocated width and capped at 640 px.
    """

    __gtype_name__ = "DeskboardVideoBlock"

    def __init__(self, block: Any, ctx: Any, post: Any = None) -> None:
        super().__init__()
        media_style.install()
        self.block = block
        self.ctx = ctx
        self.post = post
        self.plays_inline = mediasvc.direct_video_url(block)
        self.poster_url = mediasvc.video_poster_url(block)
        self.duration = mediasvc.video_duration(block)
        self.aspect = mediasvc.video_aspect(block)
        self.provider = str(getattr(block, "provider", "") or "").lower()
        self._alive = True
        self._mapped = False
        self._player: Optional[_InlineVideo] = None
        self._autoplayed = False
        self._width_hint = max(200, config.CONTENT_MAX_WIDTH)
        self._last_width = -1
        self.set_hexpand(True)
        self.set_vexpand(False)
        self.add_css_class("db-video-frame")
        try:
            self.set_overflow(Gtk.Overflow.HIDDEN)
        except Exception:  # noqa: BLE001
            pass
        data = getattr(block, "data", {}) or {}
        _paint_placeholder(self, data.get("colors") if isinstance(data, dict) else None)

        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(False)
        self.stack.set_parent(self)

        self.poster_page = Gtk.Overlay()
        self.poster_page.set_hexpand(True)
        self.poster_page.set_vexpand(False)
        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.picture.set_can_shrink(True)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(False)
        self.poster_page.set_child(self.picture)
        self.stack.add_named(self.poster_page, "poster")

        # A Gtk.Overlay so the lazily created inline player fills the page.
        self.player_page = Gtk.Overlay()
        self.player_page.set_hexpand(True)
        self.player_page.set_vexpand(False)
        self.stack.add_named(self.player_page, "player")
        self.stack.set_visible_child_name("poster")

        label = self._provider_label()
        if label:
            chip = Gtk.Label(label=label)
            chip.add_css_class("db-media-badge")
            chip.set_halign(Gtk.Align.START)
            chip.set_valign(Gtk.Align.START)
            chip.set_margin_top(8)
            chip.set_margin_start(8)
            chip.set_can_target(False)
            self.poster_page.add_overlay(chip)

        if self.duration > 0:
            duration = Gtk.Label(label=_format_seconds(self.duration))
            duration.add_css_class("db-media-badge")
            duration.set_halign(Gtk.Align.END)
            duration.set_valign(Gtk.Align.END)
            duration.set_margin_bottom(8)
            duration.set_margin_end(8)
            duration.set_can_target(False)
            self.poster_page.add_overlay(duration)

        if self.plays_inline:
            play = Gtk.Button()
            play.add_css_class("db-media-play")
            play.set_child(_icon("media-playback-start-symbolic", 26))
            play.set_halign(Gtk.Align.CENTER)
            play.set_valign(Gtk.Align.CENTER)
            play.set_tooltip_text("Play video")
            play.connect("clicked", self._on_play_clicked)
            self.play_button = play
            self.poster_page.add_overlay(play)
            _make_clickable(self.poster_page, lambda _n: self._on_play_clicked(None))
        else:
            watch = Gtk.Button()
            watch.add_css_class("pill")
            watch.set_halign(Gtk.Align.CENTER)
            watch.set_valign(Gtk.Align.CENTER)
            watch.set_label(self._watch_label())
            watch.set_tooltip_text("Open the video on the web")
            watch.connect("clicked", lambda _b: self._open_web())
            self.play_button = watch
            self.poster_page.add_overlay(watch)
            _make_clickable(self.poster_page, lambda _n: self._open_web())

        self.error_label = _dim_label("Video unavailable")
        self.error_label.set_halign(Gtk.Align.CENTER)
        self.error_label.set_valign(Gtk.Align.END)
        self.error_label.set_margin_bottom(12)
        self.error_label.set_visible(False)
        self.error_label.set_can_target(False)
        self.poster_page.add_overlay(self.error_label)

        self._load_poster()
        self.set_size_request(-1, self._height_for(self._width_hint))

    # -- geometry ---------------------------------------------------------- #
    def _height_for(self, width: int) -> int:
        width = max(width, 200)
        height = int(width / self.aspect) if self.aspect else int(width * 9 / 16)
        return int(_clamp(height, 160, _SINGLE_MAX_HEIGHT))

    def do_measure(self, orientation, for_size):  # noqa: N802 - vfunc
        if orientation == Gtk.Orientation.HORIZONTAL:
            return (1, max(1, config.CONTENT_MAX_WIDTH), -1, -1)
        height = self._height_for(self._width_hint)
        return (height, height, -1, -1)

    def do_size_allocate(self, width, height, baseline):  # noqa: N802 - vfunc
        width = max(1, int(width))
        if abs(width - self._width_hint) > 2:
            self._width_hint = width
            self.set_size_request(-1, self._height_for(width))
            self.queue_resize()
        self._last_width = width
        self.stack.allocate(width, height, baseline, None)

    def do_dispose(self) -> None:  # noqa: N802 - vfunc
        stack, self.stack = getattr(self, "stack", None), None
        if stack is not None:
            stack.unparent()
        Gtk.Widget.do_dispose(self)

    # -- poster ------------------------------------------------------------ #
    def _load_poster(self) -> None:
        if not self.poster_url:
            return

        def apply(texture) -> None:
            if not self._alive or texture is None:
                return
            try:
                self.picture.set_paintable(texture)
            except Exception:  # noqa: BLE001
                pass

        try:
            self.ctx.images.load(
                upscale_image_url(self.poster_url, 1280), 1280, 1280, apply
            )
        except Exception:  # noqa: BLE001
            pass

    # -- labels ------------------------------------------------------------ #
    def _provider_label(self) -> str:
        return _PROVIDER_LABELS.get(self.provider, self.provider.title())

    def _watch_label(self) -> str:
        label = self._provider_label()
        if self.provider in ("youtube", "vimeo"):
            return f"Watch on {label}"
        if self.provider == "tumblr":
            return "Watch on Tumblr"
        return "Watch video"

    def _open_web(self) -> None:
        url = str(getattr(self.block, "url", "") or "")
        if not url and self.post is not None:
            url = str(getattr(self.post, "post_url", "") or "")
        if url:
            try:
                self.ctx.open_url(url)
            except Exception:  # noqa: BLE001
                pass

    # -- playback ---------------------------------------------------------- #
    def _make_player(self, muted: bool) -> _InlineVideo:
        player = _InlineVideo(
            self.plays_inline,
            duration_hint=self.duration,
            muted=muted,
            on_error=self._on_player_error,
        )
        self.player_page.set_child(player)
        self._player = player
        return player

    def _on_play_clicked(self, _button) -> None:
        if not self.plays_inline:
            self._open_web()
            return
        try:
            if self._player is None:
                player = self._make_player(muted=False)
                player.resume(muted=False)
            else:
                self._player.resume(muted=False)
            self.stack.set_visible_child_name("player")
            self.error_label.set_visible(False)
        except Exception:  # noqa: BLE001
            self._open_web()

    def _on_player_error(self) -> None:
        if not self._alive:
            return
        self.stack.set_visible_child_name("poster")
        self.error_label.set_visible(True)

    # -- Gtk lifecycle ----------------------------------------------------- #
    def do_map(self) -> None:
        Gtk.Widget.do_map(self)
        self._mapped = True
        if (
            self.plays_inline
            and not self._autoplayed
            and _safe_pref(self.ctx, "autoplay_video", False)
        ):
            self._autoplayed = True
            try:
                player = self._make_player(muted=True)
                player.resume(muted=True)
                self.stack.set_visible_child_name("player")
            except Exception:  # noqa: BLE001
                pass

    def do_unmap(self) -> None:
        self._mapped = False
        if self._player is not None:
            self._player.stop_playback()
            try:
                self.stack.set_visible_child_name("poster")
            except Exception:  # noqa: BLE001
                pass
        Gtk.Widget.do_unmap(self)

    def do_unroot(self) -> None:
        self._alive = False
        if self._player is not None:
            self._player.stop_playback()
            self._player = None
        Gtk.Widget.do_unroot(self)


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #

class _AudioSession:
    """One shared audio stream so playback continues while the feed scrolls.

    Widgets attach as listeners while mapped.  When every listener is gone the
    stream gets a short grace period, then stops; this bounds the lifetime of a
    stream whose owning card was destroyed while still letting a scrolled-away
    track keep playing long enough to scroll back.
    """

    _instance: Optional["_AudioSession"] = None

    IDLE_STOP_SECONDS = 30

    @classmethod
    def get(cls) -> "_AudioSession":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._stream = None
        self._url = ""
        self._error = ""
        self._listeners: list[Callable[[dict], None]] = []
        self._ticker = 0
        self._idle_stop = 0
        self._duration = 0

    # -- listeners --------------------------------------------------------- #
    def add_listener(self, callback: Callable[[dict], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)
        self._cancel_idle_stop()
        self._notify()

    def remove_listener(self, callback: Callable[[dict], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass
        if not self._listeners:
            self._schedule_idle_stop()

    def _notify(self) -> None:
        state = self.state()
        for callback in list(self._listeners):
            try:
                callback(state)
            except Exception:  # noqa: BLE001 - dead widget
                pass

    def state(self) -> dict:
        playing = False
        timestamp = 0
        duration = self._duration
        if self._stream is not None:
            try:
                playing = bool(self._stream.get_playing())
                timestamp = int(self._stream.get_timestamp() or 0)
                duration = int(self._stream.get_duration() or 0) or duration
            except Exception:  # noqa: BLE001
                pass
        return {
            "url": self._url,
            "playing": playing,
            "timestamp": timestamp,
            "duration": duration,
            "error": self._error,
        }

    # -- transport --------------------------------------------------------- #
    def play(self, url: str) -> None:
        if not url:
            return
        if self._stream is not None and url == self._url:
            self.toggle()
            return
        self._stop_stream()
        self._url = url
        self._error = ""
        self._duration = 0
        try:
            stream = Gtk.MediaFile.new_for_file(Gio.File.new_for_uri(url))
            stream.set_loop(False)
            stream.set_muted(False)
            stream.set_volume(1.0)
            stream.connect("notify::error", self._on_error)
            stream.connect("notify::ended", self._on_ended)
            stream.connect("notify::duration", self._on_duration)
            self._stream = stream
            stream.set_playing(True)
        except Exception:  # noqa: BLE001
            self._error = "Audio unavailable"
        self._start_ticker()
        self._notify()

    def toggle(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.set_playing(not self._stream.get_playing())
        except Exception:  # noqa: BLE001
            pass
        self._notify()

    def pause(self) -> None:
        if self._stream is not None:
            try:
                self._stream.set_playing(False)
            except Exception:  # noqa: BLE001
                pass
        self._notify()

    def seek(self, microseconds: int) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(max(0, int(microseconds)))
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop_stream()
        self._url = ""
        self._error = ""
        self._duration = 0
        self._notify()

    def _stop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.set_playing(False)
                stream.pause()
            except Exception:  # noqa: BLE001
                pass
        self._stop_ticker()

    # -- timers ------------------------------------------------------------ #
    def _start_ticker(self) -> None:
        if self._ticker:
            return
        self._ticker = GLib.timeout_add(250, self._tick)

    def _stop_ticker(self) -> None:
        if self._ticker:
            try:
                GLib.source_remove(self._ticker)
            except Exception:  # noqa: BLE001
                pass
            self._ticker = 0

    def _tick(self) -> bool:
        if self._stream is None:
            self._ticker = 0
            return False
        self._notify()
        return True

    def _schedule_idle_stop(self) -> None:
        if self._stream is None or self._idle_stop:
            return
        self._idle_stop = GLib.timeout_add_seconds(
            self.IDLE_STOP_SECONDS, self._idle_stop_cb
        )

    def _cancel_idle_stop(self) -> None:
        if self._idle_stop:
            try:
                GLib.source_remove(self._idle_stop)
            except Exception:  # noqa: BLE001
                pass
            self._idle_stop = 0

    def _idle_stop_cb(self) -> bool:
        self._idle_stop = 0
        if not self._listeners:
            self.stop()
        return False

    # -- stream signals ---------------------------------------------------- #
    def _on_duration(self, stream, _pspec) -> None:
        try:
            self._duration = int(stream.get_duration() or 0)
        except Exception:  # noqa: BLE001
            pass
        self._notify()

    def _on_ended(self, stream, _pspec) -> None:
        self.stop()

    def _on_error(self, stream, _pspec) -> None:
        self._error = "Audio unavailable"
        self._stop_stream()
        self._notify()


class AudioBlock(Gtk.Box):
    """Compact inline audio player (no external window)."""

    __gtype_name__ = "DeskboardAudioBlock"

    def __init__(self, block: Any, ctx: Any, post: Any = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        media_style.install()
        self.block = block
        self.ctx = ctx
        self.post = post
        self.url = mediasvc.audio_url(block) or ""
        self.title = mediasvc.block_title(block) or "Audio post"
        self.artist = mediasvc.block_artist(block)
        self.album = mediasvc.block_album(block)
        self.poster_url = mediasvc.audio_poster_url(block)
        self.session = _AudioSession.get()
        self._attached = False
        self._alive = True
        self._seeking = False

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("db-audio-row")
        self.append(row)

        self.poster_box = _MediaBox("db-audio-poster")
        self.poster_box.set_media_size(52, 52)
        self.poster_box.set_hexpand(False)
        self.poster_box.set_vexpand(False)
        self.poster_box.set_valign(Gtk.Align.CENTER)
        self.poster_picture = Gtk.Picture()
        self.poster_picture.set_content_fit(Gtk.ContentFit.COVER)
        self.poster_picture.set_can_shrink(True)
        self.poster_picture.set_vexpand(False)
        self.poster_box.set_child(self.poster_picture)
        row.append(self.poster_box)

        middle = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        middle.set_hexpand(True)
        middle.set_valign(Gtk.Align.CENTER)
        row.append(middle)

        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label=self.title)
        title.add_css_class("db-audio-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_xalign(0)
        title.set_hexpand(True)
        heading.append(title)
        middle.append(heading)

        if self.artist:
            subtitle = Gtk.Label(label=self.artist)
            subtitle.add_css_class("dim-label")
            subtitle.add_css_class("caption")
            subtitle.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle.set_xalign(0)
            middle.append(subtitle)

        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        middle.append(transport)

        self.play_button = Gtk.Button()
        self.play_button.add_css_class("flat")
        self.play_button.set_child(_icon("media-playback-start-symbolic", 18))
        self.play_button.set_tooltip_text("Play audio")
        self.play_button.connect("clicked", self._on_play_clicked)
        transport.append(self.play_button)

        self.seek = _SeekScale(self._seek)
        transport.append(self.seek)

        self.time_label = _dim_label("0:00", css=("db-audio-time", "dim-label"))
        self.time_label.set_label("0:00")
        transport.append(self.time_label)

        if not self.url:
            self.play_button.set_sensitive(False)
            self.play_button.set_tooltip_text("Audio unavailable")
            self.time_label.set_label("Unavailable")

        if self.poster_url:
            self._load_poster()
        else:
            icon = _icon("audio-x-generic-symbolic", 24)
            icon.set_halign(Gtk.Align.CENTER)
            icon.set_valign(Gtk.Align.CENTER)
            self.poster_box.set_child(icon)

        self.session.add_listener(self._on_session_state)
        self._attached = True

    # -- poster ------------------------------------------------------------ #
    def _load_poster(self) -> None:
        def apply(texture) -> None:
            if not self._alive or texture is None:
                return
            try:
                self.poster_picture.set_paintable(texture)
            except Exception:  # noqa: BLE001
                pass

        try:
            self.ctx.images.load(
                upscale_image_url(self.poster_url, 320), 320, 320, apply
            )
        except Exception:  # noqa: BLE001
            pass

    # -- transport --------------------------------------------------------- #
    def _on_play_clicked(self, _button) -> None:
        if not self.url:
            return
        state = self.session.state()
        if state["url"] == self.url:
            self.session.toggle()
        else:
            self.session.play(self.url)

    def _seek(self, microseconds: int) -> None:
        self.session.seek(microseconds)

    def _on_session_state(self, state: dict) -> None:
        if not self._alive:
            return
        if state.get("url") != self.url:
            try:
                self.play_button.set_child(_icon("media-playback-start-symbolic", 18))
                self.seek.set_duration(0)
                self.time_label.set_label("0:00")
            except Exception:  # noqa: BLE001
                pass
            return
        duration = int(state.get("duration") or 0)
        position = int(state.get("timestamp") or 0)
        if duration > 0:
            try:
                self.seek.set_duration(duration)
            except Exception:  # noqa: BLE001
                pass
        if not self._seeking:
            try:
                self.seek.set_position(position)
            except Exception:  # noqa: BLE001
                pass
        self.time_label.set_label(f"{_format_us(position)} / {_format_us(duration)}")
        icon = (
            "media-playback-pause-symbolic"
            if state.get("playing")
            else "media-playback-start-symbolic"
        )
        try:
            self.play_button.set_child(_icon(icon, 18))
        except Exception:  # noqa: BLE001
            pass
        if state.get("error"):
            self.play_button.set_sensitive(False)
            self.time_label.set_label("Unavailable")

    # -- Gtk lifecycle ----------------------------------------------------- #
    def do_map(self) -> None:
        Gtk.Widget.do_map(self)
        if not self._attached:
            self.session.add_listener(self._on_session_state)
            self._attached = True

    def do_unmap(self) -> None:
        if self._attached:
            self.session.remove_listener(self._on_session_state)
            self._attached = False
        Gtk.Widget.do_unmap(self)

    def do_unroot(self) -> None:
        self._alive = False
        if self._attached:
            self.session.remove_listener(self._on_session_state)
            self._attached = False
        Gtk.Widget.do_unroot(self)
