"""Async image loading with a memory + disk cache.

Network fetches and decoding happen on worker threads; textures are handed back
on the GTK main loop.  The disk cache keeps the *original* bytes (content
addressed by URL) so a re-render at another size costs a cheap rescale, and a
size-capped LRU keeps the cache from growing without bound.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from .. import config

try:  # imported lazily in tests that do not need GTK
    import gi

    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("Gio", "2.0")
    from gi.repository import Gdk, GdkPixbuf, Gio, GLib

    _HAVE_GTK = True
except (ImportError, ValueError):  # pragma: no cover - headless without typelibs
    Gdk = GdkPixbuf = Gio = GLib = None  # type: ignore[assignment]
    _HAVE_GTK = False

Callback = Callable[[object], None]

_MAX_PIXBUF_LOCK = threading.Lock()
_DECODE_WIDTHS = (1280, 800, 500, 320, 128)


class ImageCache:
    def __init__(
        self,
        directory: Path,
        mem_items: int = 320,
        disk_bytes: int = 768 * 1024 * 1024,
        workers: int = 6,
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.mem_items = mem_items
        self.disk_bytes = disk_bytes
        self._memory: OrderedDict[str, object] = OrderedDict()
        self._inflight: dict[str, list[Callback]] = {}
        self._lock = threading.Lock()
        self._workers = workers
        self._queue: list[tuple[str, int, int, Callback]] = []
        self._queue_cv = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._running = True
        self._ssl = None
        self._start_threads()
        threading.Thread(target=self._trim_disk, daemon=True, name="img-trim").start()

    # -- public API -------------------------------------------------------- #
    def load(self, url: str, width: int, height: int, callback: Callback) -> None:
        """Load ``url`` scaled to fit ``width``x``height``; callback gets a texture."""
        if not url:
            self._deliver(callback, None)
            return
        key = self._key(url, width)
        with self._lock:
            cached = self._memory.get(key)
            if cached is not None:
                self._memory.move_to_end(key)
        if cached is not None:
            self._deliver(callback, cached)
            return
        with self._lock:
            if key in self._inflight:
                self._inflight[key].append(callback)
                return
            self._inflight[key] = [callback]
        with self._queue_cv:
            self._queue.append((url, width, height, callback))
            self._queue_cv.notify()

    def prefetch(self, urls: list[str], width: int = 500, height: int = 750) -> None:
        for url in urls:
            if url:
                self.load(url, width, height, lambda _texture: None)

    def cached(self, url: str, width: int) -> object | None:
        with self._lock:
            texture = self._memory.get(self._key(url, width))
            if texture is not None:
                self._memory.move_to_end(self._key(url, width))
            return texture

    def shutdown(self) -> None:
        with self._queue_cv:
            self._running = False
            self._queue_cv.notify_all()

    # -- internals --------------------------------------------------------- #
    def _key(self, url: str, width: int) -> str:
        # normalise the CDN size segment so one asset caches once per width class
        bucket = min(_DECODE_WIDTHS, key=lambda w: abs(w - width))
        return f"{url.split('?')[0]}|{bucket}"

    def _start_threads(self) -> None:
        for index in range(self._workers):
            thread = threading.Thread(
                target=self._worker, name=f"img-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _worker(self) -> None:
        while True:
            with self._queue_cv:
                while self._running and not self._queue:
                    self._queue_cv.wait(0.5)
                if not self._running:
                    return
                url, width, height, _callback = self._queue.pop(0)
            texture = None
            try:
                texture = self._fetch_and_decode(url, width, height)
            except Exception:  # noqa: BLE001 - broken image should not kill the worker
                texture = None
            key = self._key(url, width)
            with self._lock:
                callbacks = self._inflight.pop(key, [])
                if texture is not None:
                    self._memory[key] = texture
                    self._memory.move_to_end(key)
                    while len(self._memory) > self.mem_items:
                        self._memory.popitem(last=False)
            for callback in callbacks:
                self._deliver(callback, texture)

    def _deliver(self, callback: Callback, texture: object) -> None:
        def run() -> bool:
            try:
                callback(texture)
            except Exception:  # noqa: BLE001 - widget may be gone
                pass
            return False

        if _HAVE_GTK:
            GLib.idle_add(run)
        else:  # pragma: no cover
            run()

    def _disk_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return self.directory / digest[:2] / digest

    def _read_bytes(self, url: str) -> bytes | None:
        path = self._disk_path(url)
        try:
            return path.read_bytes()
        except OSError:
            pass
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": config.USER_AGENT,
                    "Referer": config.ORIGIN + "/",
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
            if len(data) < 64:
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
            return data
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _fetch_and_decode(self, url: str, width: int, height: int):
        data = self._read_bytes(url)
        if data is None:
            return None
        return decode_bytes(data, width, height)

    def _trim_disk(self) -> None:
        """Drop least-recently-used files once the cache exceeds its budget."""
        try:
            files: list[tuple[float, int, Path]] = []
            total = 0
            for path in self.directory.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
            if total <= self.disk_bytes:
                return
            files.sort()
            for _mtime, size, path in files:
                try:
                    path.unlink()
                except OSError:
                    continue
                total -= size
                if total <= self.disk_bytes:
                    break
        except OSError:
            pass


def decode_bytes(data: bytes, width: int, height: int):
    """Decode image bytes into a texture, scaled down while decoding."""
    if not _HAVE_GTK:  # pragma: no cover
        return None
    target_w = max(32, min(width, 1600)) if width > 0 else 1280
    try:
        with _MAX_PIXBUF_LOCK:
            stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(
                stream, target_w, -1, True, None
            )
        return Gdk.Texture.new_for_pixbuf(pixbuf)
    except Exception:  # noqa: BLE001 - unsupported/corrupt image
        return None


def decode_file(path: str, width: int, height: int):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return decode_bytes(data, width, height)
