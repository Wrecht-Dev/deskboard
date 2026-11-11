"""Media helpers: GIF -> looping MP4 transcoding, URLs, poster textures.

Everything here is deliberately free of widget state.  Network reads and ffmpeg
run on worker threads; the helpers that touch GTK return textures that were
handed out by :class:`deskboard.services.imagecache.ImageCache` and never decode
on their own.

The GIF pipeline is content-addressed: the cache file name is derived from the
source URL, so transcoding a GIF happens exactly once.  ``gif_to_mp4`` is the
synchronous worker primitive required by the interface contract; UI code should
use :func:`gif_to_mp4_async`, which deduplicates concurrent requests and calls
back on the GTK main loop.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .. import config
from ..models import Block, Media, get, upscale_image_url

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

GIF_CACHE_DIR = Path(config.MEDIA_CACHE_DIR) / "gifs"

_FFMPEG_LOCK = threading.Lock()
_FFMPEG_PATH: str | None = None
_FFMPEG_PROBED = False

_MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 20
_FFMPEG_TIMEOUT = 30  # the contract's 30 s ceiling for one transcode
_MIN_MP4_BYTES = 1024

_PLAYABLE_EXTENSIONS = (".mp4", ".m4v", ".webm", ".mov", ".ogv")
_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")

# Texture memory cache filled by load_poster(); poster_for() is a cheap lookup.
_POSTER_LOCK = threading.Lock()
_POSTER_TEXTURES: dict[str, Any] = {}

# In-flight transcodes, keyed by URL digest.
_GIF_LOCK = threading.Lock()
_GIF_INFLIGHT: dict[str, list[Callable[[Path | None], None]]] = {}


# --------------------------------------------------------------------------- #
# ffmpeg discovery
# --------------------------------------------------------------------------- #

def _find_ffmpeg() -> str | None:
    global _FFMPEG_PATH, _FFMPEG_PROBED
    with _FFMPEG_LOCK:
        if _FFMPEG_PROBED:
            return _FFMPEG_PATH
        _FFMPEG_PROBED = True
        candidate = shutil.which("ffmpeg")
        if candidate is None:
            for path in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
                if os.access(path, os.X_OK):
                    candidate = path
                    break
        _FFMPEG_PATH = candidate
        return candidate


def has_ffmpeg() -> bool:
    """Return True when an executable ffmpeg is available."""
    return _find_ffmpeg() is not None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _url_extension(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    if "." not in path:
        return ""
    return "." + path.rsplit(".", 1)[-1].lower()


def _is_http(url: str) -> bool:
    return url.startswith("https://") or url.startswith("http://")


def _media_dict_url(value: Any) -> str:
    """``Block.data["media"]`` is a dict in NPF payloads, a list in older ones."""
    if isinstance(value, dict):
        url = value.get("url") or get(value, "url")
        return str(url or "")
    if isinstance(value, str):
        return value
    return ""


def _looks_like_image(url: str) -> bool:
    ext = _url_extension(url)
    return ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".gifv", ".avif", ".bmp", ".pnj")


def _looks_playable(url: str) -> bool:
    return _is_http(url) and _url_extension(url) in _PLAYABLE_EXTENSIONS


def _looks_audio(url: str) -> bool:
    return _is_http(url) and _url_extension(url) in _AUDIO_EXTENSIONS


# --------------------------------------------------------------------------- #
# GIF detection
# --------------------------------------------------------------------------- #

def media_is_gif(media: Media | None) -> bool:
    """True for animated GIF uploads.

    Tumblr currently serves amplified GIFs as ``.gifv`` URLs (really GIF bytes
    with an ``image/gif`` content type), so testing ``Media.is_gif`` alone is not
    enough: it only knows ``.gif`` and an ``image/gif`` mime.
    """
    if media is None:
        return False
    try:
        if media.mime and "gif" in media.mime.lower():
            return True
    except Exception:  # noqa: BLE001 - defensive against odd payloads
        pass
    url = str(getattr(media, "url", "") or "")
    return _url_extension(url) in (".gif", ".gifv")


def block_is_gif_post(media_list: list[Media]) -> bool:
    return any(media_is_gif(media) for media in media_list or [])


# --------------------------------------------------------------------------- #
# Block helpers (video / audio)
# --------------------------------------------------------------------------- #

def _media_urls(block: Block) -> list[str]:
    """Every plausible media URL attached to a block, most specific first."""
    urls: list[str] = []
    for media in list(getattr(block, "media", []) or []):
        url = str(getattr(media, "url", "") or "")
        if url:
            urls.append(url)
    try:
        data = block.data if isinstance(block.data, dict) else {}
    except Exception:  # noqa: BLE001
        data = {}
    for key in ("url", "source_url", "video_url", "audio_url"):
        value = data.get(key)
        if isinstance(value, str) and value:
            urls.append(value)
    for key in ("media", "source", "poster"):
        urls.append(_media_dict_url(data.get(key)))
        value = data.get(key)
        if isinstance(value, list):
            for item in value:
                urls.append(_media_dict_url(item))
        elif isinstance(value, dict) and isinstance(value.get("url"), str):
            urls.append(str(value["url"]))
    # Preserve order, drop empties/duplicates.
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def block_title(block: Block) -> str:
    try:
        title = getattr(block, "title", "") or block.data.get("title") or ""
        return str(title)
    except Exception:  # noqa: BLE001
        return ""


def block_artist(block: Block) -> str:
    try:
        return str(block.data.get("artist") or "")
    except Exception:  # noqa: BLE001
        return ""


def block_album(block: Block) -> str:
    try:
        return str(block.data.get("album") or "")
    except Exception:  # noqa: BLE001
        return ""


def direct_video_url(block: Block) -> str | None:
    """A URL GStreamer can play inline, or None for embeds.

    Tumblr-hosted videos live under ``media.tumblr.com``; their fixtures use
    ``.mov`` (H.264), which GStreamer plays fine, so all common container
    extensions are accepted.  YouTube/Vimeo blocks only carry a poster and a
    watch URL, and return None here.
    """
    for url in _media_urls(block):
        if _looks_playable(url):
            return url
    return None


def video_poster_url(block: Block) -> str:
    """Best still frame for a video block."""
    try:
        poster = block.poster_url
    except Exception:  # noqa: BLE001
        poster = ""
    if poster:
        return poster
    try:
        for media in getattr(block, "poster_media", []) or []:
            url = str(getattr(media, "url", "") or "")
            if url:
                return url
    except Exception:  # noqa: BLE001
        pass
    for url in _media_urls(block):
        if _looks_like_image(url):
            return url
    return ""


def video_duration(block: Block) -> float:
    try:
        value = block.data.get("duration")
        if value is None:
            value = block.data.get("duration_seconds")
        return max(0.0, float(value or 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def video_aspect(block: Block) -> float:
    """Width/height for a video block, preferring the real media payload."""
    candidates: list[tuple[int, int]] = []
    try:
        data = block.data if isinstance(block.data, dict) else {}
    except Exception:  # noqa: BLE001
        data = {}
    for holder in (data.get("media"), data.get("poster")):
        items = holder if isinstance(holder, list) else [holder]
        for item in items:
            if isinstance(item, dict):
                try:
                    w = int(item.get("width") or 0)
                    h = int(item.get("height") or 0)
                except (TypeError, ValueError):
                    continue
                if w > 0 and h > 0:
                    candidates.append((w, h))
    for media in list(getattr(block, "media", []) or []) + list(getattr(block, "poster_media", []) or []):
        try:
            if media.width > 0 and media.height > 0:
                candidates.append((int(media.width), int(media.height)))
        except Exception:  # noqa: BLE001
            continue
    for width, height in candidates:
        aspect = width / height
        if 0.15 <= aspect <= 6.0:
            return aspect
    return 16 / 9


def audio_url(block: Block) -> str | None:
    for url in _media_urls(block):
        if _looks_audio(url):
            return url
    # Some legacy audio blocks carry a bare URL without a useful extension.
    for url in _media_urls(block):
        if _is_http(url) and not _looks_like_image(url):
            return url
    return None


def audio_poster_url(block: Block) -> str:
    try:
        poster = block.poster_url
    except Exception:  # noqa: BLE001
        poster = ""
    if poster:
        return poster
    for url in _media_urls(block):
        if _looks_like_image(url):
            return url
    return ""


# --------------------------------------------------------------------------- #
# Placeholder colours
# --------------------------------------------------------------------------- #

def normalise_hex(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text.lower()


def _luminance(hex_color: str) -> float:
    try:
        r = int(hex_color[0:2], 16) / 255
        g = int(hex_color[2:4], 16) / 255
        b = int(hex_color[4:6], 16) / 255
    except (ValueError, IndexError):
        return 0.5
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def placeholder_color(colors: Any) -> str | None:
    """Pick a palette entry that reads as a neutral tile behind a photo.

    Very dark entries disappear into a dark theme and very light ones glare in a
    light theme, so mid-luminance shades win.  ``None`` means "use the theme's
    card shade" (handled by CSS).
    """
    if not isinstance(colors, dict):
        return None
    candidates: list[str] = []
    for key in ("c0", "c1", "c2", "c3", "c4"):
        colour = normalise_hex(colors.get(key))
        if colour:
            candidates.append(colour)
    for colour in candidates:
        if 0.16 <= _luminance(colour) <= 0.72:
            return colour
    if candidates:
        # No mid-tone in the palette: take the one closest to neutral instead of
        # the first entry, which is usually near-black.
        return min(candidates, key=lambda value: abs(_luminance(value) - 0.45))
    return None


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #

def _download(url: str, limit: int = _MAX_DOWNLOAD_BYTES) -> bytes | None:
    if not _is_http(url):
        return None
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": config.USER_AGENT,
            "Referer": config.ORIGIN + "/",
            "Accept": "image/gif,image/webp,image/png,image/jpeg,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)
            return data if len(data) >= 64 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def download_bytes(url: str, limit: int = _MAX_DOWNLOAD_BYTES) -> bytes | None:
    """Fetch a small media file into memory (worker-thread helper)."""
    return _download(url, limit=limit)


# --------------------------------------------------------------------------- #
# GIF -> MP4
# --------------------------------------------------------------------------- #

def gif_cache_path(url: str) -> Path:
    digest = _digest(url)
    return GIF_CACHE_DIR / digest[:2] / f"{digest}.mp4"


def _cached_mp4(url: str) -> Path | None:
    path = gif_cache_path(url)
    try:
        if path.is_file() and path.stat().st_size >= _MIN_MP4_BYTES:
            return path
    except OSError:
        pass
    return None


def gif_to_mp4(url: str) -> Path | None:
    """Transcode an animated GIF URL to a cached, silent, looping H.264 MP4.

    Synchronous by contract: call it from a worker thread.  Returns the cache
    path on success and ``None`` when ffmpeg is missing, the download failed or
    the transcode timed out (30 s).
    """
    if not url:
        return None
    cached = _cached_mp4(url)
    if cached is not None:
        return cached

    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        return None

    data = _download(url)
    if not data:
        return None

    destination = gif_cache_path(url)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    temporary = destination.with_name(
        f".{destination.stem}.{os.getpid()}.{threading.get_ident()}.part.mp4"
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", "pipe:0",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "24",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30",
        "-movflags", "+faststart",
        str(temporary),
    ]
    try:
        result = subprocess.run(
            command,
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_FFMPEG_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _unlink(temporary)
        return None

    try:
        if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size >= _MIN_MP4_BYTES:
            temporary.replace(destination)
            return destination
    except OSError:
        pass
    _unlink(temporary)
    return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _main_loop_deliver(callback: Callable[..., None], *args: Any) -> None:
    def run() -> bool:
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - the widget may be gone
            pass
        return False

    try:
        from gi.repository import GLib

        GLib.idle_add(run)
    except Exception:  # noqa: BLE001 - no GTK (tests / CLI): call inline
        run()


def gif_to_mp4_async(url: str, callback: Callable[[Path | None], None]) -> None:
    """Transcode ``url`` in the background; ``callback(path_or_none)`` on the main loop."""
    if not url:
        _main_loop_deliver(callback, None)
        return
    cached = _cached_mp4(url)
    if cached is not None:
        _main_loop_deliver(callback, cached)
        return

    key = _digest(url)
    with _GIF_LOCK:
        waiting = _GIF_INFLIGHT.get(key)
        if waiting is not None:
            waiting.append(callback)
            return
        _GIF_INFLIGHT[key] = [callback]

    def work() -> None:
        path: Path | None = None
        try:
            path = gif_to_mp4(url)
        except Exception:  # noqa: BLE001 - a bad GIF must not kill the worker
            path = None
        with _GIF_LOCK:
            waiters = _GIF_INFLIGHT.pop(key, [])
        for waiter in waiters:
            _main_loop_deliver(waiter, path)

    threading.Thread(target=work, daemon=True, name="gif-transcode").start()


# --------------------------------------------------------------------------- #
# Posters (cheap synchronous access to textures that ctx.images already loaded)
# --------------------------------------------------------------------------- #

def media_poster_url(media: Media | None) -> str:
    """Best still image URL associated with a media object."""
    if media is None:
        return ""
    poster = str(getattr(media, "poster", "") or "")
    if poster:
        return poster
    url = str(getattr(media, "url", "") or "")
    return url if _looks_like_image(url) else ""


def remember_poster(url: str, texture: Any) -> None:
    """Remember a texture produced by :data:`ctx.images` for :func:`poster_for`."""
    if not url or texture is None:
        return
    with _POSTER_LOCK:
        if len(_POSTER_TEXTURES) > 256:
            # Cheap FIFO trim; posters are tiny and re-fetchable.
            for key in list(_POSTER_TEXTURES)[:64]:
                _POSTER_TEXTURES.pop(key, None)
        _POSTER_TEXTURES[url] = texture


def poster_for(media: Media | Block | str | None) -> Any:
    """Return an already-loaded poster texture, or ``None``.

    Textures are only ever produced by the shared image cache, so this is a
    synchronous lookup, not a fetch: call :func:`load_poster` (which uses
    ``ctx.images.load``) first, or let :class:`VideoBlock` do it.
    """
    if isinstance(media, str):
        url = media
    else:
        try:
            poster = getattr(media, "poster_url", "")
        except Exception:  # noqa: BLE001
            poster = ""
        url = poster or media_poster_url(media)  # type: ignore[arg-type]
    if not url:
        return None
    with _POSTER_LOCK:
        return _POSTER_TEXTURES.get(url)


def load_poster(media: Media | Block | str, ctx: Any, width: int = 800,
                callback: Callable[[Any], None] | None = None) -> None:
    """Load a poster through ``ctx.images`` and remember it for ``poster_for``."""
    if isinstance(media, str):
        url = media
    else:
        try:
            url = getattr(media, "poster_url", "") or media_poster_url(media)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            url = ""
    if not url:
        if callback is not None:
            callback(None)
        return

    def apply(texture: Any) -> None:
        if texture is not None:
            remember_poster(url, texture)
        if callback is not None:
            callback(texture)

    try:
        ctx.images.load(upscale_image_url(url, width), width, width, apply)
    except Exception:  # noqa: BLE001 - no cache in tests
        if callback is not None:
            callback(None)


__all__ = [
    "audio_poster_url",
    "audio_url",
    "block_album",
    "block_artist",
    "block_is_gif_post",
    "block_title",
    "direct_video_url",
    "download_bytes",
    "gif_cache_path",
    "gif_to_mp4",
    "gif_to_mp4_async",
    "has_ffmpeg",
    "load_poster",
    "media_is_gif",
    "media_poster_url",
    "normalise_hex",
    "placeholder_color",
    "poster_for",
    "remember_poster",
    "video_aspect",
    "video_duration",
    "video_poster_url",
]
