"""Application-wide constants and XDG paths for Deskboard."""
from __future__ import annotations

import os
from pathlib import Path

APP_ID = "dev.wrecht.Deskboard"
APP_NAME = "Deskboard"
APP_TAGLINE = "A fast, native Tumblr dashboard"
APP_VERSION = "0.1.0"

# Tumblr's own website talks to this origin for its internal ("kraken") API.
ORIGIN = "https://www.tumblr.com"
API_BASE = ORIGIN + "/api"

# Public bearer token served inside Tumblr's own web bootstrap payload. It is the
# same credential the website hands every visitor, so it authorises exactly what a
# logged-out browser can see. A personal session cookie unlocks the rest.
DEFAULT_API_TOKEN = ""

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# How many posts to ask for per page.
PAGE_SIZE = 20


def _xdg(env: str, default: str) -> Path:
    raw = os.environ.get(env)
    base = Path(raw).expanduser() if raw else Path(default).expanduser()
    return base / "deskboard"


DATA_DIR = _xdg("XDG_DATA_HOME", "~/.local/share")
CACHE_DIR = _xdg("XDG_CACHE_HOME", "~/.cache")
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", "~/.config")

IMAGE_CACHE_DIR = CACHE_DIR / "images"
MEDIA_CACHE_DIR = CACHE_DIR / "media"
COOKIE_FILE = DATA_DIR / "cookies.json"
PREFS_FILE = CONFIG_DIR / "preferences.json"

# Text sizing / layout knobs used by the post renderers.
CONTENT_MAX_WIDTH = 760
AVATAR_SIZE = 40


def ensure_dirs() -> None:
    for path in (DATA_DIR, CACHE_DIR, CONFIG_DIR, IMAGE_CACHE_DIR, MEDIA_CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)


class Preferences:
    """Tiny persisted preferences store (JSON, auto-saved)."""

    DEFAULTS = {
        "window_width": 1120,
        "window_height": 820,
        "window_maximized": False,
        "nav_collapsed": False,
        "open_links_in_app": False,
        "autoplay_video": False,
        "show_avatars": True,
        "compact": False,
        "nsfw_blur": True,
        "theme": "system",  # system | light | dark
        "last_feed": "dashboard",
    }

    def __init__(self) -> None:
        import json

        self._json = json
        self.values = dict(self.DEFAULTS)
        try:
            loaded = json.loads(PREFS_FILE.read_text("utf-8"))
            if isinstance(loaded, dict):
                self.values.update({k: v for k, v in loaded.items() if k in self.DEFAULTS})
        except (OSError, ValueError):
            pass

    def get(self, key, default=None):
        return self.values.get(key, default if default is not None else self.DEFAULTS.get(key))

    def set(self, key, value) -> None:
        self.values[key] = value
        self.save()

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            PREFS_FILE.write_text(self._json.dumps(self.values, indent=2), "utf-8")
        except OSError:
            pass
