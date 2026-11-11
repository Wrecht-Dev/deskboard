"""Full-window photo viewer with navigation, zoom, pan and save.

Owner: media.  ``open_viewer(ctx, medias, index=0, post=None)`` is the public
entry point used by :class:`ImageGrid`.

Textures come from ``ctx.images`` at a high-resolution width; loading never
blocks the main loop and callbacks are generation-checked so fast navigation or
a closed window cannot paint a stale image.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk, Pango

from .. import config
from ..models import upscale_image_url
from ..services import media as mediasvc
from . import media_style

_REQUEST_WIDTH = 1280
_LOAD_WIDTH = 1600
_MIN_ZOOM = 1.0
_MAX_ZOOM = 8.0
_SWIPE_THRESHOLD = 70


# --------------------------------------------------------------------------- #
# Zoom / pan canvas
# --------------------------------------------------------------------------- #

class _ZoomCanvas(Gtk.Widget):
    """Draws a texture with contain-fit, an optional zoom factor and pan offset."""

    __gtype_name__ = "DeskboardZoomCanvas"

    def __init__(self) -> None:
        super().__init__()
        self.texture: Any = None
        self.zoom = _MIN_ZOOM
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.set_hexpand(True)
        self.set_vexpand(True)

    # -- geometry ---------------------------------------------------------- #
    def do_measure(self, orientation, for_size):  # noqa: N802 - vfunc
        # The viewer gives us the whole window; a tiny minimum keeps the window
        # from collapsing before the window manager applies the default size.
        return (1, 1, -1, -1)

    def _draw_size(self) -> tuple[float, float, float, float]:
        texture = self.texture
        if texture is None:
            return (0.0, 0.0, 0.0, 0.0)
        width = float(self.get_width())
        height = float(self.get_height())
        try:
            tex_width = float(texture.get_width())
            tex_height = float(texture.get_height())
        except Exception:  # noqa: BLE001
            return (0.0, 0.0, 0.0, 0.0)
        if tex_width <= 0 or tex_height <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        scale = min(width / tex_width, height / tex_height) * self.zoom
        draw_width = tex_width * scale
        draw_height = tex_height * scale
        return (draw_width, draw_height, width, height)

    def _clamp_pan(self) -> None:
        draw_width, draw_height, width, height = self._draw_size()
        max_x = max(0.0, (draw_width - width) / 2.0)
        max_y = max(0.0, (draw_height - height) / 2.0)
        self.pan_x = max(-max_x, min(max_x, self.pan_x))
        self.pan_y = max(-max_y, min(max_y, self.pan_y))
        if self.zoom <= _MIN_ZOOM + 0.001:
            self.pan_x = 0.0
            self.pan_y = 0.0

    # -- public API -------------------------------------------------------- #
    def set_texture(self, texture: Any) -> None:
        self.texture = texture
        self.queue_draw()

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(_MIN_ZOOM, min(_MAX_ZOOM, float(zoom)))
        self._clamp_pan()
        self.queue_draw()

    def zoom_by(self, factor: float) -> None:
        self.set_zoom(self.zoom * factor)

    def reset(self) -> None:
        self.zoom = _MIN_ZOOM
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.queue_draw()

    def set_pan(self, x: float, y: float) -> None:
        self.pan_x = float(x)
        self.pan_y = float(y)
        self._clamp_pan()
        self.queue_draw()

    def get_pan(self) -> tuple[float, float]:
        return (self.pan_x, self.pan_y)

    # -- rendering --------------------------------------------------------- #
    def do_snapshot(self, snapshot):  # noqa: N802 - vfunc
        if self.texture is None:
            return
        draw_width, draw_height, width, height = self._draw_size()
        if draw_width <= 0 or draw_height <= 0:
            return
        x = (width - draw_width) / 2.0 + self.pan_x
        y = (height - draw_height) / 2.0 + self.pan_y
        snapshot.append_texture(
            self.texture, Graphene.Rect().init(x, y, draw_width, draw_height)
        )


# --------------------------------------------------------------------------- #
# Viewer window
# --------------------------------------------------------------------------- #

class _Viewer(Gtk.Window):
    __gtype_name__ = "DeskboardViewer"

    def __init__(self, ctx: Any, medias: list[Any], index: int, post: Any = None) -> None:
        super().__init__()
        media_style.install()
        self.ctx = ctx
        self.medias = medias
        self.post = post
        self._index = max(0, min(int(index or 0), len(medias) - 1))
        self._alive = True
        self._generation = 0
        self._textures: dict[int, Any] = {}
        self._drag_start = (0.0, 0.0)

        self.set_title("Photo")
        self.add_css_class("db-viewer-window")
        try:
            self.set_modal(True)
        except Exception:  # noqa: BLE001
            pass
        parent = getattr(ctx, "window", None)
        if isinstance(parent, Gtk.Window):
            try:
                self.set_transient_for(parent)
                width = parent.get_width() or 1120
                height = parent.get_height() or 820
                self.set_default_size(max(640, int(width)), max(480, int(height)))
            except Exception:  # noqa: BLE001
                self.set_default_size(1120, 820)
        else:
            self.set_default_size(1120, 820)

        header = Adw.HeaderBar()
        header.add_css_class("db-viewer-header")
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        self.header = header

        self.close_button = self._header_button(
            "window-close-symbolic", "Close (Esc)", self.close
        )
        header.pack_start(self.close_button)
        self.prev_button = self._header_button(
            "go-previous-symbolic", "Previous (Left)", lambda: self._step(-1)
        )
        self.next_button = self._header_button(
            "go-next-symbolic", "Next (Right)", lambda: self._step(1)
        )
        header.pack_start(self.prev_button)
        header.pack_start(self.next_button)

        self.window_title = Adw.WindowTitle(title="1 / 1", subtitle="")
        header.set_title_widget(self.window_title)

        self.zoom_label = Gtk.Label(label="100%")
        self.zoom_label.add_css_class("db-viewer-zoomlabel")
        self.zoom_label.set_margin_start(6)
        self.zoom_label.set_margin_end(6)
        header.pack_end(self.zoom_label)
        self.zoom_out_button = self._header_button(
            "zoom-out-symbolic", "Zoom out (-)", lambda: self._zoom_by(1 / 1.4)
        )
        header.pack_end(self.zoom_out_button)
        self.zoom_in_button = self._header_button(
            "zoom-in-symbolic", "Zoom in (+)", lambda: self._zoom_by(1.4)
        )
        header.pack_end(self.zoom_in_button)
        self.zoom_reset_button = self._header_button(
            "zoom-original-symbolic", "Actual size / reset (0)", self._zoom_reset
        )
        header.pack_end(self.zoom_reset_button)

        self.save_button = self._header_button(
            "document-save-symbolic", "Save to Pictures (Ctrl+S)", self._save
        )
        header.pack_end(self.save_button)
        self.open_button = self._header_button(
            "web-browser-symbolic", "Open in browser", self._open_browser
        )
        header.pack_end(self.open_button)

        self.set_titlebar(header)

        self.overlay = Gtk.Overlay()
        self.overlay.set_hexpand(True)
        self.overlay.set_vexpand(True)
        self.set_child(self.overlay)

        self.canvas = _ZoomCanvas()
        self.overlay.set_child(self.canvas)

        # Static loading label, not a Gtk.Spinner: the image cache delivers
        # textures from a default-idle source, which an always-animating spinner
        # can starve because the frame clock outranks idle callbacks.
        self.loading = Gtk.Label(label="Loading…")
        self.loading.add_css_class("db-viewer-status")
        self.loading.set_halign(Gtk.Align.CENTER)
        self.loading.set_valign(Gtk.Align.CENTER)
        self.overlay.add_overlay(self.loading)

        self.status = Gtk.Label(label="")
        self.status.add_css_class("db-viewer-status")
        self.status.set_halign(Gtk.Align.CENTER)
        self.status.set_valign(Gtk.Align.CENTER)
        self.status.set_visible(False)
        self.overlay.add_overlay(self.status)

        left_zone = self._nav_zone("go-previous-symbolic", Gtk.Align.START, -1)
        right_zone = self._nav_zone("go-next-symbolic", Gtk.Align.END, 1)
        self.overlay.add_overlay(left_zone)
        self.overlay.add_overlay(right_zone)

        self.caption = Gtk.Label(label="")
        self.caption.add_css_class("db-viewer-caption")
        self.caption.set_halign(Gtk.Align.CENTER)
        self.caption.set_valign(Gtk.Align.END)
        self.caption.set_margin_bottom(12)
        self.caption.set_ellipsize(Pango.EllipsizeMode.END)
        self.caption.set_max_width_chars(100)
        self.caption.set_can_target(False)
        self.overlay.add_overlay(self.caption)

        self._install_controllers()
        self.connect("close-request", self._on_close_request)
        self.connect("notify::default-width", lambda *_a: self.canvas.queue_draw())
        self.connect("notify::default-height", lambda *_a: self.canvas.queue_draw())

        self._update_header()
        self._show_index(self._index)

    # -- construction helpers --------------------------------------------- #
    def _header_button(self, icon: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("flat")
        button.set_child(Gtk.Image.new_from_icon_name(icon))
        button.set_tooltip_text(tooltip)
        button.connect("clicked", lambda _b: callback())
        return button

    def _nav_zone(self, icon: str, align: Gtk.Align, direction: int) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("db-viewer-zone")
        button.set_child(Gtk.Image.new_from_icon_name(icon))
        button.set_halign(align)
        button.set_valign(Gtk.Align.FILL)
        button.set_hexpand(False)
        button.set_vexpand(True)
        button.set_tooltip_text("Previous image" if direction < 0 else "Next image")
        button.connect("clicked", lambda _b: self._step(direction))
        return button

    def _install_controllers(self) -> None:
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.canvas.add_controller(scroll)

        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.canvas.add_controller(drag)

        try:
            swipe = Gtk.GestureSwipe()
            swipe.connect("swipe", self._on_swipe)
            self.canvas.add_controller(swipe)
        except Exception:  # noqa: BLE001 - optional nicety
            pass

    # -- image loading ----------------------------------------------------- #
    def _current_media(self):
        if 0 <= self._index < len(self.medias):
            return self.medias[self._index]
        return None

    def _show_index(self, index: int) -> None:
        if not self.medias:
            return
        index = max(0, min(int(index), len(self.medias) - 1))
        self._index = index
        self._generation += 1
        generation = self._generation
        media = self._current_media()
        self._update_header()

        cached = self._textures.get(index)
        if cached is not None:
            self.canvas.set_texture(cached)
            self.loading.set_visible(False)
            self.status.set_visible(False)
            self._prefetch_neighbours(index)
            return

        self.canvas.set_texture(None)
        self.canvas.reset()
        self.status.set_visible(False)
        self.loading.set_visible(True)

        url = getattr(media, "url", "") if media is not None else ""
        if not url:
            self._show_error("Image unavailable")
            return
        request_url = upscale_image_url(url, _REQUEST_WIDTH)

        def apply(texture) -> None:
            if generation != self._generation or not self._alive:
                return
            self.loading.set_visible(False)
            if texture is None:
                self._show_error("Image unavailable")
                return
            self._textures[index] = texture
            self.canvas.set_texture(texture)
            self.canvas.reset()
            self._update_zoom_label()

        try:
            self.ctx.images.load(request_url, _LOAD_WIDTH, _LOAD_WIDTH, apply)
        except Exception:  # noqa: BLE001
            self._show_error("Image unavailable")
            return
        self._prefetch_neighbours(index)

    def _prefetch_neighbours(self, index: int) -> None:
        for neighbour in (index - 1, index + 1):
            if not 0 <= neighbour < len(self.medias):
                continue
            if neighbour in self._textures:
                continue
            media = self.medias[neighbour]
            url = getattr(media, "url", "") or ""
            if not url:
                continue
            try:
                self.ctx.images.load(
                    upscale_image_url(url, _REQUEST_WIDTH),
                    _LOAD_WIDTH,
                    _LOAD_WIDTH,
                    lambda _texture: None,
                )
            except Exception:  # noqa: BLE001
                pass

    def _show_error(self, message: str) -> None:
        self.loading.set_visible(False)
        self.canvas.set_texture(None)
        self.status.set_label(message)
        self.status.set_visible(True)

    def _update_header(self) -> None:
        media = self._current_media()
        count = len(self.medias)
        title = f"{self._index + 1} / {count}" if count else "No photos"
        subtitle = ""
        if self.post is not None:
            subtitle = str(getattr(self.post, "display_name", "") or "")
        elif media is not None:
            subtitle = str(getattr(media, "alt", "") or "")
        try:
            self.window_title.set_title(title)
            self.window_title.set_subtitle(subtitle)
        except Exception:  # noqa: BLE001
            pass
        self.set_title(f"{title} — Deskboard")
        try:
            self.prev_button.set_sensitive(self._index > 0)
            self.next_button.set_sensitive(self._index < count - 1)
        except Exception:  # noqa: BLE001
            pass
        caption = ""
        if media is not None:
            caption = str(getattr(media, "alt", "") or "")
        self.caption.set_label(caption)
        self.caption.set_visible(bool(caption))
        self._update_zoom_label()

    def _update_zoom_label(self) -> None:
        try:
            self.zoom_label.set_label(f"{int(round(self.canvas.zoom * 100))}%")
        except Exception:  # noqa: BLE001
            pass

    # -- navigation -------------------------------------------------------- #
    def _step(self, delta: int) -> None:
        target = self._index + delta
        if 0 <= target < len(self.medias):
            self._show_index(target)

    def _on_swipe(self, _gesture, velocity_x, _velocity_y) -> None:
        if abs(velocity_x) < 0.1:
            return
        self._step(1 if velocity_x < 0 else -1)

    def _on_drag_begin(self, gesture, _x, _y) -> None:
        self._drag_start = self.canvas.get_pan()

    def _on_drag_update(self, gesture, offset_x, offset_y) -> None:
        if self.canvas.zoom > _MIN_ZOOM + 0.001:
            self.canvas.set_pan(
                self._drag_start[0] + offset_x, self._drag_start[1] + offset_y
            )

    def _on_drag_end(self, gesture, offset_x, offset_y) -> None:
        if self.canvas.zoom <= _MIN_ZOOM + 0.001 and abs(offset_x) > _SWIPE_THRESHOLD:
            self._step(1 if offset_x < 0 else -1)

    # -- zoom -------------------------------------------------------------- #
    def _zoom_by(self, factor: float) -> None:
        self.canvas.zoom_by(factor)
        self._update_zoom_label()

    def _zoom_reset(self) -> None:
        self.canvas.reset()
        self._update_zoom_label()

    def _on_scroll(self, _controller, _dx, dy) -> bool:
        if dy == 0:
            return False
        self._zoom_by(1.18 if dy < 0 else 1 / 1.18)
        return True

    # -- keyboard ---------------------------------------------------------- #
    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        name = Gdk.keyval_name(keyval) or ""
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and name.lower() == "s":
            self._save()
            return True
        if name == "Escape":
            self.close()
            return True
        if name in ("Left", "Up", "Page_Up", "KP_Left"):
            self._step(-1)
            return True
        if name in ("Right", "Down", "Page_Down", "KP_Right", "space"):
            self._step(1)
            return True
        if name == "Home":
            self._show_index(0)
            return True
        if name == "End":
            self._show_index(len(self.medias) - 1)
            return True
        if name in ("plus", "equal", "KP_Add"):
            self._zoom_by(1.4)
            return True
        if name in ("minus", "KP_Subtract"):
            self._zoom_by(1 / 1.4)
            return True
        if name in ("0", "KP_0"):
            self._zoom_reset()
            return True
        if name.lower() == "o":
            self._open_browser()
            return True
        return False

    # -- save / open ------------------------------------------------------- #
    def _open_browser(self) -> None:
        url = ""
        if self.post is not None:
            url = str(getattr(self.post, "post_url", "") or "")
        if not url:
            media = self._current_media()
            url = str(getattr(media, "url", "") or "")
        if not url:
            self._toast("No link for this image")
            return
        try:
            self.ctx.open_url(url)
        except Exception:  # noqa: BLE001
            self._toast("Could not open the browser")

    def _save(self) -> None:
        media = self._current_media()
        url = str(getattr(media, "url", "") or "")
        if not url:
            self._toast("Nothing to save")
            return
        index = self._index
        post = self.post
        self.save_button.set_sensitive(False)
        self._toast("Saving…")

        def work() -> None:
            result = _save_media(url, index, post)
            GLib.idle_add(lambda: self._save_done(result))

        threading.Thread(target=work, daemon=True, name="media-save").start()

    def _save_done(self, result: tuple[bool, str]) -> bool:
        ok, message = result
        try:
            self.save_button.set_sensitive(True)
        except Exception:  # noqa: BLE001
            pass
        self._toast(message)
        return False

    def _toast(self, message: str) -> None:
        try:
            self.ctx.toast(message)
        except Exception:  # noqa: BLE001
            pass

    # -- lifecycle --------------------------------------------------------- #
    def _on_close_request(self, _window) -> bool:
        self._alive = False
        self._textures.clear()
        try:
            self.canvas.set_texture(None)
        except Exception:  # noqa: BLE001
            pass
        return False

    def do_unroot(self) -> None:  # noqa: N802 - vfunc
        self._alive = False
        Gtk.Widget.do_unroot(self)


# --------------------------------------------------------------------------- #
# Saving (worker thread; Gio copy, no shell)
# --------------------------------------------------------------------------- #

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _pictures_directory() -> str:
    try:
        directory = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        if directory:
            return str(directory)
    except Exception:  # noqa: BLE001
        pass
    return str(Path.home() / "Pictures")


def _filename_for(url: str, index: int, post: Any) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    basename = path.rsplit("/", 1)[-1]
    extension = Path(basename).suffix.lower()
    if extension not in (".jpg", ".jpeg", ".png", ".gif", ".gifv", ".webp", ".avif"):
        extension = ".jpg"
    stem = ""
    if post is not None:
        blog = str(getattr(post, "blog_name", "") or "")
        post_id = str(getattr(post, "id", "") or "")
        if blog or post_id:
            stem = f"tumblr-{blog}-{post_id}".strip("-")
    if not stem:
        stem = f"deskboard-{Path(basename).stem or 'image'}"
    stem = _SAFE_NAME.sub("-", stem).strip(".-") or "deskboard-image"
    return f"{stem}-{index + 1}{extension}"


def _unique_destination(directory: Gio.File, filename: str) -> Gio.File:
    candidate = directory.get_child(filename)
    if not candidate.query_exists(None):
        return candidate
    path = Path(filename)
    for number in range(1, 1000):
        candidate = directory.get_child(f"{path.stem}-{number}{path.suffix}")
        if not candidate.query_exists(None):
            return candidate
    return directory.get_child(f"{path.stem}-{int(GLib.get_real_time())}{path.suffix}")


def _save_media(url: str, index: int, post: Any) -> tuple[bool, str]:
    data = mediasvc.download_bytes(url)
    if not data:
        return (False, "Could not download the image")
    directory_path = _pictures_directory()
    directory = Gio.File.new_for_path(directory_path)
    try:
        directory.make_directory_with_parents(None)
    except GLib.Error:
        pass  # already exists
    except Exception:  # noqa: BLE001
        return (False, "Could not open the Pictures folder")
    destination = _unique_destination(directory, _filename_for(url, index, post))

    temp_dir = Path(config.MEDIA_CACHE_DIR) / "saves"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return (False, "Could not write the image")
    temp_path = temp_dir / f"save-{threading.get_ident()}-{GLib.get_real_time()}.tmp"
    try:
        temp_path.write_bytes(data)
        source = Gio.File.new_for_path(str(temp_path))
        source.copy(destination, Gio.FileCopyFlags.NONE, None, None)
    except GLib.Error:
        return (False, "Could not copy the image into Pictures")
    except OSError:
        return (False, "Could not write the image")
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass
    return (True, f"Saved to {destination.get_path() or directory_path}")


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def open_viewer(ctx: Any, medias: Any, index: int = 0, post: Any = None):
    """Open the full-window photo viewer; returns the window (or ``None``)."""
    media_style.install()
    cleaned = [
        media
        for media in (medias or [])
        if getattr(media, "url", "")
    ]
    if not cleaned:
        try:
            ctx.toast("Nothing to show")
        except Exception:  # noqa: BLE001
            pass
        return None
    viewer = _Viewer(ctx, cleaned, int(index or 0), post)
    try:
        setattr(ctx, "_media_viewer", viewer)
    except Exception:  # noqa: BLE001
        pass
    viewer.present()
    return viewer
