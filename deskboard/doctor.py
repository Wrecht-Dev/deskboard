"""``python3 -m deskboard --doctor``: environment and connectivity checks."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _ok(label: str, detail: str = "") -> None:
    print(f"  \033[32m✓\033[0m {label}" + (f"  {detail}" if detail else ""))


def _warn(label: str, detail: str = "") -> None:
    print(f"  \033[33m!\033[0m {label}" + (f"  {detail}" if detail else ""))


def _bad(label: str, detail: str = "") -> None:
    print(f"  \033[31m✗\033[0m {label}" + (f"  {detail}" if detail else ""))


def run_doctor() -> int:
    from . import config

    problems = 0

    print(f"\n{config.APP_NAME} {config.APP_VERSION} — environment check\n")

    print("Runtime")
    _ok("Python", sys.version.split()[0])
    try:
        import gi

        _ok("PyGObject", f"{gi.__version__} (GObject introspection)")
    except ImportError:
        _bad("PyGObject", "python3-gi is not installed")
        return 1

    gtk_ok = adw_ok = False
    try:
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        gtk_ok = True
        _ok("GTK", f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
    except (ImportError, ValueError) as exc:
        _bad("GTK 4", f"not importable — run scripts/bootstrap-gir.sh ({exc})")
        problems += 1
    try:
        gi.require_version("Adw", "1")
        from gi.repository import Adw

        adw_ok = True
        _ok("libadwaita", f"{Adw.get_major_version()}.{Adw.get_minor_version()}")
    except (ImportError, ValueError) as exc:
        _bad("libadwaita", f"not importable — run scripts/bootstrap-gir.sh ({exc})")
        problems += 1

    typelib = os.environ.get("GI_TYPELIB_PATH", "")
    if typelib:
        _ok("GI_TYPELIB_PATH", typelib)
    elif (Path.home() / ".local/share/deskboard/gir").is_dir():
        _warn("GI_TYPELIB_PATH", "set by scripts/env.sh — source it (or use scripts/run.sh)")
    print()

    print("Display")
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        _ok("Display", os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY", ""))
    else:
        _warn("Display", "headless session — the window cannot be opened here")
    print()

    print("Media")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        _ok("ffmpeg", ffmpeg + " (animated GIF playback)")
    else:
        _warn("ffmpeg", "missing — GIFs will show a still frame")
    try:
        import ctypes.util

        if ctypes.util.find_library("gstreamer-1.0"):
            _ok("GStreamer", "video/audio playback available")
        else:
            _warn("GStreamer", "missing — video/audio will show posters only")
    except Exception:  # noqa: BLE001 - optional dependency
        _warn("GStreamer", "could not be probed")
    print()

    print("Session")
    from .api.auth import SessionManager, find_browser_candidates

    session = SessionManager()
    if session.cookies:
        _ok("Cookies", f"{len(session.cookies)} cookies from {session.source or 'store'}")
        _ok("Session file", str(session.store))
    else:
        _warn("Cookies", "no session stored")
        candidates = find_browser_candidates()
        if candidates:
            print(f"      browsers with profiles available: {', '.join(candidates)}")
        print("      sign in from the app, or run: deskboard --import-cookies <file>")
    print()

    print("Network")
    from .api.client import TumblrClient, TumblrError

    client = TumblrClient(cookies=session.cookies)
    try:
        page = client.get_blog_posts("staff", limit=2)
        _ok("Tumblr API (public)", f"{len(page.posts)} posts fetched")
    except TumblrError as exc:
        _bad("Tumblr API (public)", str(exc))
        problems += 1
    if session.cookies:
        try:
            info = client.get_user_info()
            _ok("Signed in", f"@{info.get('name', '?')}")
        except TumblrError as exc:
            _warn("Session invalid", str(exc))
            problems += 1
        try:
            page = client.get_dashboard(limit=3)
            _ok("Dashboard", f"{len(page.posts)} posts, paging={'yes' if page.next_url else 'no'}")
        except TumblrError as exc:
            _bad("Dashboard", str(exc))
            problems += 1
    print()

    print("Storage")
    config.ensure_dirs()
    _ok("Data", str(config.DATA_DIR))
    _ok("Cache", str(config.CACHE_DIR))
    try:
        total = sum(p.stat().st_size for p in config.IMAGE_CACHE_DIR.rglob("*") if p.is_file())
        _ok("Image cache", f"{total / 1_048_576:.1f} MiB in {config.IMAGE_CACHE_DIR}")
    except OSError:
        _warn("Image cache", "unreadable")
    print()

    if problems:
        print(f"{problems} problem(s) found. Fix the ✗ items above and re-run.\n")
        return 1
    print("Everything looks good.\n")
    return 0
