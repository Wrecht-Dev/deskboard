"""Shared application context passed to every widget.

Widgets never talk to the network directly: they use ``ctx.client`` (via feed
sources) and ``ctx.images`` for pictures, and they publish changes through the
small pub/sub so several cards can stay in sync (e.g. a like toggled in one
place updates the same post elsewhere).
"""
from __future__ import annotations

import subprocess
import threading
from typing import Any, Callable

from . import config
from .api.auth import SessionManager
from .api.client import TumblrClient
from .services.imagecache import ImageCache

Listener = Callable[..., None]


class AppContext:
    """Everything a widget needs, and nothing it should not touch."""

    def __init__(self, demo: bool = False, cookies_path: str | None = None) -> None:
        config.ensure_dirs()
        self.demo = demo
        self.prefs = config.Preferences()
        self.session = SessionManager(store=cookies_path) if cookies_path else SessionManager()
        self.images = ImageCache(config.IMAGE_CACHE_DIR)

        if demo:
            from .demo import DemoClient

            self.client: TumblrClient = DemoClient()  # type: ignore[assignment]
            self.user_name = "demo"
            self.user_blog = "demo"
            self.user_blogs: list[str] = ["demo"]
        else:
            self.client = TumblrClient(
                cookies=self.session.cookies,
                on_auth_failed=self._refresh_session_from_browser,
            )
            self.user_name = ""
            self.user_blog = ""
            self.user_blogs = []

        self.window: Any = None
        self._listeners: dict[str, list[Listener]] = {}
        self._toast: Callable[[str], None] | None = None
        self._user_lock = threading.Lock()

    # -- pub/sub ----------------------------------------------------------- #
    def subscribe(self, event: str, callback: Listener) -> Callable[[], None]:
        self._listeners.setdefault(event, []).append(callback)

        def unsubscribe() -> None:
            try:
                self._listeners.get(event, []).remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def publish(self, event: str, *args: Any) -> None:
        for callback in list(self._listeners.get(event, [])):
            try:
                callback(*args)
            except Exception:  # noqa: BLE001 - a dead widget must not break others
                pass

    # -- user interface feedback ------------------------------------------- #
    def set_toast_handler(self, handler: Callable[[str], None] | None) -> None:
        self._toast = handler

    def toast(self, message: str) -> None:
        if self._toast:
            try:
                self._toast(message)
            except Exception:  # noqa: BLE001
                pass

    # -- session ----------------------------------------------------------- #
    @property
    def is_signed_in(self) -> bool:
        return bool(self.session.cookies)

    def adopt_session(self, cookies: dict[str, str], source: str = "") -> None:
        self.session.cookies = dict(cookies)
        self.session.source = source or self.session.source
        self.session.save()
        self.client.set_cookies(self.session.cookies)
        self.publish("session-changed", True)

    def sign_out(self) -> None:
        self.session.clear()
        self.client.set_cookies({})
        self.user_name = ""
        self.user_blog = ""
        self.user_blogs = []
        self.publish("session-changed", False)

    def _refresh_session_from_browser(self) -> dict[str, str] | None:
        """Tumblr rotates session cookies; re-read the browser store once on a 401."""
        source = (self.session.source or "").lower()
        if not source:
            return None
        result = None
        try:
            if "firefox" in source:
                result = self.session.import_firefox()
            elif any(name in source for name in ("chrom", "chrome", "brave", "vivaldi", "edge")):
                result = self.session.import_chromium()
        except Exception:  # noqa: BLE001 - refresh must never break a request
            return None
        if result and result.ok and result.count:
            self.publish("session-refreshed", self.session.cookies)
            return dict(self.session.cookies)
        return None

    def load_user(self, on_done: Callable[[bool], None] | None = None) -> None:
        """Fetch the signed-in account in the background."""

        def work() -> None:
            ok = False
            try:
                info = self.client.get_user_info()
                name = str(info.get("name") or "")
                blogs = [
                    str(item.get("name") or "")
                    for item in info.get("blogs") or []
                    if isinstance(item, dict) and item.get("name")
                ]
                with self._user_lock:
                    self.user_name = name
                    self.user_blogs = blogs
                    self.user_blog = blogs[0] if blogs else name
                ok = bool(name)
            except Exception:  # noqa: BLE001 - offline or expired session
                ok = False
            from gi.repository import GLib

            GLib.idle_add(lambda: (on_done(ok) if on_done else None, False)[1])

        threading.Thread(target=work, daemon=True, name="user-load").start()

    # -- shell helpers ----------------------------------------------------- #
    def open_url(self, url: str) -> None:
        if not url:
            return
        try:
            from gi.repository import Gtk

            Gtk.UriLauncher.new(url).launch(self.window, None, None, None)
        except Exception:  # noqa: BLE001 - fall back to xdg-open
            try:
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.toast("No application available to open that link.")

    def copy_text(self, text: str) -> None:
        try:
            import gi

            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk

            display = Gdk.Display.get_default()
            if display:
                display.get_clipboard().set(text)
                self.toast("Copied to clipboard")
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self) -> None:
        self.images.shutdown()
