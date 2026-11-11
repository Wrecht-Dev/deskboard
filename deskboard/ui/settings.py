"""Preferences window and theme application.

Owner: shell.
"""
from __future__ import annotations

import os
import shutil
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from .. import config

_THEME_KEYS = ("system", "light", "dark")
_THEME_INDEX = {"system": 0, "light": 1, "dark": 2}


def apply_theme(ctx) -> None:
    """Apply the persisted color scheme through :class:`Adw.StyleManager`."""
    try:
        value = str(ctx.prefs.get("theme", "system") or "system").lower()
        manager = Adw.StyleManager.get_default()
        if value == "light":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        elif value == "dark":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        else:
            manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
    except Exception:  # noqa: BLE001 - theming must never break startup
        pass


def _format_size(size: int) -> str:
    size = max(0, int(size))
    if size < 1024:
        return f"{size} B"
    for unit in ("KB", "MB", "GB"):
        size /= 1024.0
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}".replace(".0 ", " ")
    return f"{size:.1f} GB"


# Both the photo cache and the GIF/transcode cache are user-clearable.
_CACHE_ROOTS = (config.IMAGE_CACHE_DIR, config.MEDIA_CACHE_DIR)


def _tree_size(root) -> int:
    total = 0
    try:
        for directory, _dirs, files in os.walk(root):
            for name in files:
                try:
                    path = os.path.join(directory, name)
                    if not os.path.islink(path):
                        total += os.path.getsize(path)
                except OSError:
                    continue
    except OSError:
        return total
    return total


class SettingsWindow(Adw.PreferencesWindow):
    __gtype_name__ = "DeskboardSettingsWindow"

    def __init__(self, ctx) -> None:
        super().__init__(title="Preferences", search_enabled=True)
        self.ctx = ctx
        self._unsubscribe = None
        try:
            self.set_default_size(640, 720)
        except Exception:  # noqa: BLE001
            pass

        self._build_appearance()
        self._build_storage()
        self._build_account()

        try:
            self._unsubscribe = ctx.subscribe("session-changed", self._on_session_changed)
        except Exception:  # noqa: BLE001
            self._unsubscribe = None
        try:
            self.connect("destroy", self._on_destroy)
        except Exception:  # noqa: BLE001
            pass

    # -- appearance -------------------------------------------------------- #
    def _build_appearance(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Appearance")
        page.set_icon_name("applications-graphics-symbolic")
        self.appearance_page = page

        theme_group = Adw.PreferencesGroup()
        theme_group.set_title("Theme")
        theme_group.set_description("Follow the desktop or force a scheme for Deskboard.")

        self.theme_row = Adw.ComboRow()
        self.theme_row.set_title("Color scheme")
        self.theme_row.set_subtitle("System follows your desktop setting")
        model = Gtk.StringList.new(["System", "Light", "Dark"])
        self.theme_row.set_model(model)
        value = str(self.ctx.prefs.get("theme", "system") or "system").lower()
        self.theme_row.set_selected(_THEME_INDEX.get(value, 0))
        self.theme_row.connect("notify::selected", self._on_theme_selected)
        theme_group.add(self.theme_row)
        page.add(theme_group)

        feed_group = Adw.PreferencesGroup()
        feed_group.set_title("Feed")

        self.show_avatars_row = Adw.SwitchRow()
        self.show_avatars_row.set_title("Show avatars")
        self.show_avatars_row.set_subtitle("Display blog avatars in post headers")
        self.show_avatars_row.set_active(bool(self.ctx.prefs.get("show_avatars", True)))
        self.show_avatars_row.connect(
            "notify::active", lambda *_a: self._set_pref("show_avatars", self.show_avatars_row.get_active())
        )
        feed_group.add(self.show_avatars_row)

        self.compact_row = Adw.SwitchRow()
        self.compact_row.set_title("Compact layout")
        self.compact_row.set_subtitle("Tighter spacing, more posts on screen")
        self.compact_row.set_active(bool(self.ctx.prefs.get("compact", False)))
        self.compact_row.connect(
            "notify::active", lambda *_a: self._set_pref("compact", self.compact_row.get_active())
        )
        feed_group.add(self.compact_row)

        self.nsfw_row = Adw.SwitchRow()
        self.nsfw_row.set_title("Blur sensitive media")
        self.nsfw_row.set_subtitle("Hide posts flagged as sensitive until you reveal them")
        self.nsfw_row.set_active(bool(self.ctx.prefs.get("nsfw_blur", True)))
        self.nsfw_row.connect(
            "notify::active", lambda *_a: self._set_pref("nsfw_blur", self.nsfw_row.get_active())
        )
        feed_group.add(self.nsfw_row)

        self.autoplay_row = Adw.SwitchRow()
        self.autoplay_row.set_title("Autoplay video")
        self.autoplay_row.set_subtitle("Start videos automatically while scrolling")
        self.autoplay_row.set_active(bool(self.ctx.prefs.get("autoplay_video", False)))
        self.autoplay_row.connect(
            "notify::active", lambda *_a: self._set_pref("autoplay_video", self.autoplay_row.get_active())
        )
        feed_group.add(self.autoplay_row)
        page.add(feed_group)
        self.add(page)

    def _on_theme_selected(self, *_args) -> None:
        try:
            index = int(self.theme_row.get_selected())
        except (TypeError, ValueError):
            index = 0
        value = _THEME_KEYS[index] if 0 <= index < len(_THEME_KEYS) else "system"
        try:
            self.ctx.prefs.set("theme", value)
        except Exception:  # noqa: BLE001
            pass
        apply_theme(self.ctx)

    def _set_pref(self, key: str, value) -> None:
        try:
            self.ctx.prefs.set(key, value)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.ctx.publish("prefs-changed", key)
        except Exception:  # noqa: BLE001
            pass

    # -- storage ----------------------------------------------------------- #
    def _build_storage(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Storage")
        page.set_icon_name("drive-harddisk-symbolic")
        self.storage_page = page

        group = Adw.PreferencesGroup()
        group.set_title("Cached media")
        group.set_description("Images and GIF transcodes are cached on disk so scrolling stays fast.")

        self.cache_row = Adw.ActionRow()
        self.cache_row.set_title("Cached images")
        self.cache_row.set_subtitle("Calculating…")
        self.cache_clear_button = Gtk.Button(label="Clear")
        self.cache_clear_button.set_valign(Gtk.Align.CENTER)
        self.cache_clear_button.add_css_class("flat")
        self.cache_clear_button.set_tooltip_text("Delete every cached image")
        self.cache_clear_button.connect("clicked", self._on_clear_cache)
        self.cache_row.add_suffix(self.cache_clear_button)
        group.add(self.cache_row)
        page.add(group)
        self.add(page)
        self._refresh_cache_size()

    def _refresh_cache_size(self) -> None:
        try:
            self.cache_row.set_subtitle("Calculating…")
        except Exception:  # noqa: BLE001
            pass
        def work() -> None:
            total = sum(_tree_size(root) for root in _CACHE_ROOTS)
            try:
                GLib.idle_add(self._apply_cache_size, total)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="cache-size").start()
        except Exception:  # noqa: BLE001
            self._apply_cache_size(0)

    def _apply_cache_size(self, total: int, extra: str = "") -> bool:
        try:
            text = f"{_format_size(total)} used"
            if extra:
                text = f"{text} · {extra}"
            self.cache_row.set_subtitle(text)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _on_clear_cache(self, *_args) -> None:
        try:
            self.cache_clear_button.set_sensitive(False)
            self.cache_row.set_subtitle("Clearing…")
        except Exception:  # noqa: BLE001
            pass
        def work() -> None:
            freed = 0
            failures = 0
            entries = []
            for root in _CACHE_ROOTS:
                try:
                    root.mkdir(parents=True, exist_ok=True)
                    entries.extend(list(root.iterdir()))
                except OSError:
                    continue
            for entry in entries:
                try:
                    if entry.is_symlink() or entry.is_file():
                        size = entry.stat().st_size
                        entry.unlink()
                        freed += size
                    elif entry.is_dir():
                        freed += _tree_size(entry)
                        shutil.rmtree(entry)
                except OSError:
                    failures += 1
            try:
                GLib.idle_add(self._finish_clear_cache, freed, failures)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="cache-clear").start()
        except Exception as exc:  # noqa: BLE001
            self._finish_clear_cache(0, 1)

    def _finish_clear_cache(self, freed: int, failures: int) -> bool:
        try:
            self.cache_clear_button.set_sensitive(True)
            message = f"Cleared {_format_size(freed)} of cached media"
            if failures:
                message += f" ({failures} entries could not be removed)"
            self.ctx.toast(message)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_cache_size()
        return False

    # -- account ----------------------------------------------------------- #
    def _build_account(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Account")
        page.set_icon_name("system-users-symbolic")
        self.account_page = page

        group = Adw.PreferencesGroup()
        group.set_title("Tumblr session")
        self.account_row = Adw.ActionRow()
        self.account_row.set_title("Session")
        self.account_row.set_subtitle("…")
        group.add(self.account_row)
        page.add(group)

        self.account_button = Gtk.Button(label="Import")
        self.account_button.set_valign(Gtk.Align.CENTER)
        self.account_button.connect("clicked", self._on_account_button)
        self.account_row.add_suffix(self.account_button)

        about_group = Adw.PreferencesGroup()
        about_group.set_title("About")
        about_row = Adw.ActionRow()
        about_row.set_title(config.APP_NAME)
        about_row.set_subtitle(f"Version {config.APP_VERSION}")
        about_button = Gtk.Button(label="About")
        about_button.set_valign(Gtk.Align.CENTER)
        about_button.add_css_class("flat")
        about_button.connect("clicked", self._on_about_clicked)
        about_row.add_suffix(about_button)
        about_group.add(about_row)
        page.add(about_group)

        self.add(page)
        self._update_account()

    def _update_account(self) -> None:
        try:
            signed_in = bool(self.ctx.is_signed_in) or bool(getattr(self.ctx, "demo", False))
            if signed_in:
                if getattr(self.ctx, "demo", False):
                    subtitle = "Demo mode — sample data"
                else:
                    source = str(getattr(self.ctx.session, "source", "") or "")
                    subtitle = f"Signed in as {self.ctx.user_name}" if self.ctx.user_name else "Signed in"
                    if source:
                        subtitle += f" · cookies from {source}"
                self.account_row.set_subtitle(subtitle)
                self.account_button.set_label("Sign out")
                self.account_button.remove_css_class("suggested-action")
                self.account_button.add_css_class("flat")
            else:
                self.account_row.set_subtitle("Browsing as a guest — public feeds only")
                self.account_button.set_label("Import cookies")
                self.account_button.remove_css_class("flat")
                self.account_button.add_css_class("suggested-action")
        except Exception:  # noqa: BLE001
            pass

    def _on_account_button(self, *_args) -> None:
        window = getattr(self.ctx, "window", None)
        try:
            if bool(self.ctx.is_signed_in) or bool(getattr(self.ctx, "demo", False)):
                if window is not None:
                    window.activate_action("win.sign-out", None)
            else:
                self.close()
                if window is not None:
                    window.show_onboarding()
        except Exception:  # noqa: BLE001
            pass

    def _on_about_clicked(self, *_args) -> None:
        try:
            window = getattr(self.ctx, "window", None)
            if window is not None:
                window.activate_action("win.about", None)
        except Exception:  # noqa: BLE001
            pass

    def _on_session_changed(self, *_args) -> None:
        self._update_account()

    def _on_destroy(self, *_args) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._unsubscribe = None
