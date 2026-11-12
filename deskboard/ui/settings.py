"""Preferences window: appearance, storage, blogs, account and read-only
Tumblr settings.

Owner: shell.  Nothing here writes to Tumblr; account/blog/user settings are
read-only until their write routes are verified.
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
from ..util import short_number

_THEME_KEYS = ("system", "light", "dark")
_THEME_INDEX = {"system": 0, "light": 1, "dark": 2}
_CHROMIUM_HINTS = ("chrom", "chrome", "brave", "vivaldi", "edge", "opera")


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


def _humanize(key: str) -> str:
    text = str(key or "").replace("_", " ")
    out: list[str] = []
    for index, char in enumerate(text):
        if char.isupper() and index:
            out.append(" ")
        out.append(char.lower())
    value = "".join(out).strip()
    return (value[:1].upper() + value[1:]) if value else "Setting"


def _display(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "On" if value else "Off"
    text = str(value)
    return text if text else "—"


class SettingsWindow(Adw.PreferencesWindow):
    __gtype_name__ = "DeskboardSettingsWindow"

    def __init__(self, ctx) -> None:
        super().__init__(title="Preferences", search_enabled=True)
        self.ctx = ctx
        self._unsubscribe = None
        self._blogs_generation = 0
        self._blog_settings_generation = 0
        self._user_settings_generation = 0
        self._reimport_busy = False
        self._syncing_blog_selector = False
        self._blog_names: list[str] = []
        self._blog_infos: dict[str, object] = {}
        self._blog_rows: dict[str, Adw.ActionRow] = {}
        self._blog_avatars: dict[str, Adw.Avatar] = {}
        self._blog_checks: dict[str, Gtk.Image] = {}
        self._blog_setting_rows: dict[str, tuple[Adw.ActionRow, Gtk.Label, Gtk.Button]] = {}
        self._group_widgets: dict[int, list[Gtk.Widget]] = {}
        try:
            self.set_default_size(680, 760)
        except Exception:  # noqa: BLE001
            pass

        self._build_blogs()
        self._build_blog_settings()
        self._build_account()
        self._build_user_settings()
        self._build_appearance()
        self._build_storage()

        try:
            self._unsubscribe = ctx.subscribe("session-changed", self._on_session_changed)
        except Exception:  # noqa: BLE001
            self._unsubscribe = None
        try:
            self.connect("close-request", self._on_close_request)
        except Exception:  # noqa: BLE001
            pass

    # -- helpers ----------------------------------------------------------- #
    def _remember_widget(self, group, widget) -> None:
        widgets = self._group_widgets.setdefault(id(group), [])
        if widget not in widgets:
            widgets.append(widget)

    def _clear_group(self, group) -> None:
        for widget in self._group_widgets.pop(id(group), []):
            try:
                group.remove(widget)
            except Exception:  # noqa: BLE001
                pass

    def _posting_blog(self) -> str:
        try:
            blogs = [str(name) for name in (self.ctx.user_blogs or []) if name]
        except Exception:  # noqa: BLE001
            blogs = []
        stored = ""
        try:
            stored = str(self.ctx.prefs.get("posting_blog", "") or "")
        except Exception:  # noqa: BLE001
            stored = ""
        if stored and stored in blogs:
            return stored
        if stored and blogs:
            # A renamed/deleted blog should not linger as the posting identity.
            try:
                self.ctx.prefs.set("posting_blog", "")
            except Exception:  # noqa: BLE001
                pass
        primary = str(getattr(self.ctx, "user_blog", "") or "")
        if primary in blogs:
            return primary
        return blogs[0] if blogs else primary

    def _blog_url(self, name: str) -> str:
        info = self._blog_infos.get(name)
        try:
            url = str(getattr(info, "web_url", "") or "")
        except Exception:  # noqa: BLE001
            url = ""
        return url or (f"https://{name}.tumblr.com" if name else "")

    def _add_value_row(self, group, title: str, value: str = "—", *,
                       multiline: bool = False):
        row = Adw.ActionRow(title=title)
        label = Gtk.Label(label=value or "—", xalign=0)
        label.set_selectable(True)
        label.set_wrap(multiline)
        label.set_max_width_chars(38)
        label.add_css_class("dim-label")
        row.add_suffix(label)
        copy = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        copy.add_css_class("flat")
        copy.set_valign(Gtk.Align.CENTER)
        copy.set_tooltip_text("Copy value")
        copy.connect(
            "clicked",
            lambda _button, target=label: self.ctx.copy_text(target.get_text() or ""),
        )
        copy.set_sensitive(bool(value and value != "—"))
        row.add_suffix(copy)
        group.add(row)
        self._remember_widget(group, row)
        return row, label, copy

    @staticmethod
    def _set_value(label: Gtk.Label, copy: Gtk.Button, value) -> None:
        text = _display(value)
        try:
            label.set_text(text)
            copy.set_sensitive(text != "—")
        except Exception:  # noqa: BLE001
            pass

    # -- blogs ------------------------------------------------------------- #
    def _build_blogs(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Blogs")
        page.set_icon_name("system-users-symbolic")
        self.blogs_page = page

        self.blogs_group = Adw.PreferencesGroup()
        self.blogs_group.set_title("Your blogs")
        self.blogs_group.set_description(
            "Choose the blog Deskboard posts and replies as."
        )
        page.add(self.blogs_group)

        self.blogs_error_group = Adw.PreferencesGroup()
        self.blogs_error_row = Adw.ActionRow()
        self.blogs_error_row.set_title("Couldn't load blog details")
        self.blogs_error_row.set_subtitle("")
        retry = Gtk.Button(label="Retry")
        retry.add_css_class("flat")
        retry.set_valign(Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_args: self._load_blog_infos())
        self.blogs_error_row.add_suffix(retry)
        self.blogs_error_group.add(self.blogs_error_row)
        self.blogs_error_group.set_visible(False)
        page.add(self.blogs_error_group)

        self.add(page)
        self._render_blog_rows()

    def _render_blog_rows(self) -> None:
        self._clear_group(self.blogs_group)
        self._blog_rows.clear()
        self._blog_avatars.clear()
        self._blog_checks.clear()
        try:
            names = []
            for name in self.ctx.user_blogs or []:
                name = str(name or "").strip()
                if name and name not in names:
                    names.append(name)
        except Exception:  # noqa: BLE001
            names = []
        self._blog_names = names
        if not names:
            row = Adw.ActionRow()
            row.set_title("No blogs yet")
            row.set_subtitle("Sign in to see the blogs on your account.")
            self.blogs_group.add(row)
            self._remember_widget(self.blogs_group, row)
            self.blogs_error_group.set_visible(False)
            return

        for name in names:
            row = Adw.ActionRow()
            row.set_title(name)
            row.set_subtitle(f"@{name}.tumblr.com · Loading details…")
            row.set_activatable(True)
            row.set_tooltip_text("Use this blog for posting and replies")
            avatar = Adw.Avatar(size=40, text=name, show_initials=True)
            avatar.set_valign(Gtk.Align.CENTER)
            row.add_prefix(avatar)
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_valign(Gtk.Align.CENTER)
            check.set_tooltip_text("Current posting blog")
            check.set_visible(False)
            row.add_suffix(check)
            copy = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
            copy.add_css_class("flat")
            copy.set_valign(Gtk.Align.CENTER)
            copy.set_tooltip_text("Copy blog URL")
            copy.connect("clicked", lambda _b, n=name: self.ctx.copy_text(self._blog_url(n)))
            row.add_suffix(copy)
            open_button = Gtk.Button.new_from_icon_name("web-browser-symbolic")
            open_button.add_css_class("flat")
            open_button.set_valign(Gtk.Align.CENTER)
            open_button.set_tooltip_text("Open in browser")
            open_button.connect(
                "clicked", lambda _b, n=name: self.ctx.open_url(self._blog_url(n))
            )
            row.add_suffix(open_button)
            row.connect("activated", lambda _r, n=name: self._select_posting_blog(n))
            self.blogs_group.add(row)
            self._remember_widget(self.blogs_group, row)
            self._blog_rows[name] = row
            self._blog_avatars[name] = avatar
            self._blog_checks[name] = check
            info = self._blog_infos.get(name)
            if info is not None:
                self._apply_blog_row(name, info)
        self._update_blog_checks()
        self._load_blog_infos()

    def _apply_blog_row(self, name: str, info) -> None:
        row = self._blog_rows.get(name)
        if row is None:
            return
        try:
            title = str(getattr(info, "display_name", "") or name)
            followers = int(getattr(info, "followers", 0) or 0)
            posts = int(getattr(info, "posts", 0) or 0)
            row.set_title(title)
            row.set_subtitle(
                f"@{name}.tumblr.com · {short_number(followers)} followers · "
                f"{short_number(posts)} posts"
            )
        except Exception:  # noqa: BLE001
            pass
        avatar_url = str(getattr(info, "avatar", "") or "")
        avatar = self._blog_avatars.get(name)
        if avatar_url and avatar is not None:
            def apply(texture) -> None:
                try:
                    if texture is not None and avatar.get_root() is not None:
                        avatar.set_custom_image(texture)
                except Exception:  # noqa: BLE001
                    pass

            try:
                self.ctx.images.load(avatar_url, 64, 64, apply)
            except Exception:  # noqa: BLE001
                pass

    def _update_blog_checks(self) -> None:
        selected = self._posting_blog()
        for name, check in self._blog_checks.items():
            try:
                check.set_visible(name == selected)
            except Exception:  # noqa: BLE001
                pass

    def _select_posting_blog(self, name: str) -> None:
        name = str(name or "").strip()
        if not name:
            return
        try:
            self.ctx.prefs.set("posting_blog", name)
        except Exception as exc:  # noqa: BLE001
            self.ctx.toast(f"Couldn't save the posting blog: {exc}")
            return
        self._update_blog_checks()
        try:
            self.ctx.publish("posting-blog-changed", name)
        except Exception:  # noqa: BLE001
            pass
        self.ctx.toast(f"Posting as @{name}")

    def _load_blog_infos(self) -> None:
        if not self._blog_names:
            return
        self._blogs_generation += 1
        generation = self._blogs_generation
        self.blogs_error_group.set_visible(False)
        names = list(self._blog_names)
        for name in names:
            row = self._blog_rows.get(name)
            if row is not None and name not in self._blog_infos:
                try:
                    row.set_subtitle(f"@{name}.tumblr.com · Loading details…")
                except Exception:  # noqa: BLE001
                    pass
        client = self.ctx.client

        def work() -> None:
            infos: dict[str, object] = {}
            failures: list[tuple[str, str]] = []
            for name in names:
                try:
                    infos[name] = client.get_blog_info(name)
                except Exception as exc:  # noqa: BLE001
                    failures.append((name, " ".join(str(exc).split())[:240]))
            try:
                GLib.idle_add(self._apply_blog_infos, generation, infos, failures)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="settings-blogs").start()
        except Exception as exc:  # noqa: BLE001
            self._apply_blog_infos(generation, {}, [("", str(exc))])

    def _apply_blog_infos(self, generation: int, infos: dict, failures: list) -> bool:
        if generation != self._blogs_generation:
            return False
        self._blog_infos.update(infos)
        for name, info in infos.items():
            self._apply_blog_row(name, info)
        if failures:
            name, message = failures[0]
            self.blogs_error_row.set_subtitle(
                (f"@{name}: {message}" if name else message)
            )
            self.blogs_error_group.set_visible(True)
            if len(failures) > 1:
                self.blogs_error_row.set_subtitle(
                    f"{self.blogs_error_row.get_subtitle()} (and {len(failures) - 1} more)"
                )
        else:
            self.blogs_error_group.set_visible(False)
        if infos:
            self._refresh_blog_settings_model()
        return False

    # -- blog settings (read-only) ----------------------------------------- #
    def _build_blog_settings(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Blog settings")
        page.set_icon_name("emblem-system-symbolic")
        self.blog_settings_page = page

        self.blog_settings_selector_group = Adw.PreferencesGroup()
        self.blog_settings_selector_group.set_title("Blog")
        self.blog_settings_selector = Adw.ComboRow()
        self.blog_settings_selector.set_title("Choose a blog")
        self.blog_settings_selector.set_subtitle("Read-only blog configuration")
        self.blog_settings_selector.connect(
            "notify::selected", self._on_blog_settings_selected
        )
        self.blog_settings_selector_group.add(self.blog_settings_selector)
        page.add(self.blog_settings_selector_group)

        self.blog_settings_group = Adw.PreferencesGroup()
        self.blog_settings_group.set_title("Settings")
        self.blog_settings_group.set_description(
            "Read-only for now — editing needs the next phase "
            "(write route not verified)."
        )
        page.add(self.blog_settings_group)
        for key, title, multiline in (
            ("name", "Name", False),
            ("title", "Title", False),
            ("description", "Description", True),
            ("cname", "Custom domain", False),
            ("truncateRssFeed", "RSS truncation", False),
        ):
            self._blog_setting_rows[key] = self._add_value_row(
                self.blog_settings_group, title, "—", multiline=multiline
            )

        self.blog_settings_error_group = Adw.PreferencesGroup()
        self.blog_settings_error_row = Adw.ActionRow()
        self.blog_settings_error_row.set_title("Couldn't load blog settings")
        retry = Gtk.Button(label="Retry")
        retry.add_css_class("flat")
        retry.set_valign(Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_args: self._load_blog_settings())
        self.blog_settings_error_row.add_suffix(retry)
        self.blog_settings_error_group.add(self.blog_settings_error_row)
        self.blog_settings_error_group.set_visible(False)
        page.add(self.blog_settings_error_group)

        self.blog_settings_unavailable_group = Adw.PreferencesGroup()
        self.blog_settings_unavailable_row = Adw.ActionRow()
        self.blog_settings_unavailable_row.set_title("Not available in demo mode")
        self.blog_settings_unavailable_row.set_subtitle(
            "Blog settings need a signed-in Tumblr session."
        )
        self.blog_settings_unavailable_group.add(self.blog_settings_unavailable_row)
        self.blog_settings_unavailable_group.set_visible(False)
        page.add(self.blog_settings_unavailable_group)

        self.add(page)
        self._refresh_blog_settings_model()

    def _refresh_blog_settings_model(self) -> None:
        names = list(self._blog_names)
        if not names:
            self.blog_settings_selector_group.set_visible(False)
            self.blog_settings_group.set_visible(False)
            self._show_blog_settings_unavailable(
                "No blogs on this account",
                "Sign in to inspect your blog settings.",
                retry=False,
            )
            return
        self._syncing_blog_selector = True
        try:
            model = Gtk.StringList.new(names)
            self.blog_settings_selector.set_model(model)
            selected = self._posting_blog()
            index = names.index(selected) if selected in names else 0
            self.blog_settings_selector.set_selected(index)
            self.blog_settings_selector.set_sensitive(True)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._syncing_blog_selector = False
        if bool(getattr(self.ctx, "demo", False)):
            self.blog_settings_selector_group.set_visible(False)
            self.blog_settings_group.set_visible(False)
            self._show_blog_settings_unavailable(
                "Not available in demo mode",
                "Blog settings need a signed-in Tumblr session.",
                retry=False,
            )
            return
        self.blog_settings_selector_group.set_visible(True)
        self.blog_settings_group.set_visible(True)
        self.blog_settings_unavailable_group.set_visible(False)
        self._load_blog_settings()

    def _on_blog_settings_selected(self, *_args) -> None:
        if self._syncing_blog_selector:
            return
        self._load_blog_settings()

    def _show_blog_settings_unavailable(self, title: str, body: str, *, retry: bool) -> None:
        self.blog_settings_error_group.set_visible(False)
        self.blog_settings_unavailable_row.set_title(title)
        self.blog_settings_unavailable_row.set_subtitle(body)
        self.blog_settings_unavailable_group.set_visible(True)
        for key, (row, label, copy) in self._blog_setting_rows.items():
            self._set_value(label, copy, "—")

    def _load_blog_settings(self) -> None:
        if bool(getattr(self.ctx, "demo", False)):
            return
        try:
            index = int(self.blog_settings_selector.get_selected())
        except (TypeError, ValueError):
            index = 0
        if not (0 <= index < len(self._blog_names)):
            return
        name = self._blog_names[index]
        self._blog_settings_generation += 1
        generation = self._blog_settings_generation
        self.blog_settings_error_group.set_visible(False)
        self.blog_settings_unavailable_group.set_visible(False)
        self.blog_settings_group.set_visible(True)
        for _key, (_row, label, copy) in self._blog_setting_rows.items():
            self._set_value(label, copy, "Loading…")
        client = self.ctx.client

        def work() -> None:
            error = ""
            settings = {}
            try:
                settings = client.get_blog_settings(name)
            except Exception as exc:  # noqa: BLE001
                error = " ".join(str(exc).split()) or "Tumblr refused the request."
            try:
                GLib.idle_add(self._apply_blog_settings, generation, name, settings, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="settings-blog").start()
        except Exception as exc:  # noqa: BLE001
            self._apply_blog_settings(generation, name, {}, str(exc))

    def _apply_blog_settings(self, generation: int, name: str, settings: dict, error: str) -> bool:
        if generation != self._blog_settings_generation:
            return False
        if error:
            self.blog_settings_error_row.set_subtitle(error)
            self.blog_settings_error_group.set_visible(True)
            self.blog_settings_group.set_visible(False)
            return False
        self.blog_settings_error_group.set_visible(False)
        self.blog_settings_unavailable_group.set_visible(False)
        self.blog_settings_group.set_visible(True)
        data = settings if isinstance(settings, dict) else {}
        values = {
            "name": data.get("name") or name,
            "title": data.get("title"),
            "description": data.get("description"),
            "cname": data.get("cname"),
            "truncateRssFeed": data.get("truncateRssFeed"),
        }
        for key, value in values.items():
            entry = self._blog_setting_rows.get(key)
            if entry is None:
                continue
            row, label, copy = entry
            self._set_value(label, copy, value)
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

        self.account_button = Gtk.Button(label="Import")
        self.account_button.set_valign(Gtk.Align.CENTER)
        self.account_button.connect("clicked", self._on_account_button)
        self.account_row.add_suffix(self.account_button)

        self.reimport_row = Adw.ActionRow()
        self.reimport_row.set_title("Re-import session")
        self.reimport_row.set_subtitle("Re-read Tumblr cookies from a browser profile")
        self.reimport_button = Gtk.Button(label="Re-import")
        self.reimport_button.add_css_class("flat")
        self.reimport_button.set_valign(Gtk.Align.CENTER)
        self.reimport_button.connect("clicked", self._on_reimport)
        self.reimport_row.add_suffix(self.reimport_button)
        group.add(self.reimport_row)

        self.reimport_error_row = Adw.ActionRow()
        self.reimport_error_row.set_title("Couldn't re-import the session")
        retry = Gtk.Button(label="Retry")
        retry.add_css_class("flat")
        retry.set_valign(Gtk.Align.CENTER)
        retry.connect("clicked", self._on_reimport)
        self.reimport_error_row.add_suffix(retry)
        self.reimport_error_row.set_visible(False)
        group.add(self.reimport_error_row)
        page.add(group)

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
            demo = bool(getattr(self.ctx, "demo", False))
            signed_in = bool(self.ctx.is_signed_in)
            if demo:
                self.account_row.set_subtitle("Demo mode — sample data")
                self.account_button.set_label("Sign out")
                self.account_button.remove_css_class("suggested-action")
                self.account_button.add_css_class("flat")
            elif signed_in:
                source = str(getattr(self.ctx.session, "source", "") or "")
                subtitle = (
                    f"Signed in as {self.ctx.user_name}" if self.ctx.user_name else "Signed in"
                )
                if source:
                    subtitle += f" · cookies from {source}"
                self.account_row.set_subtitle(subtitle)
                self.account_button.set_label("Sign out")
                self.account_button.remove_css_class("suggested-action")
                self.account_button.add_css_class("flat")
            else:
                self.account_row.set_subtitle(
                    "Browsing as a guest — public feeds only"
                )
                self.account_button.set_label("Import cookies")
                self.account_button.remove_css_class("flat")
                self.account_button.add_css_class("suggested-action")
            if demo:
                self.reimport_row.set_subtitle(
                    "Demo mode — browser cookies are not used."
                )
            elif signed_in:
                source = str(getattr(self.ctx.session, "source", "") or "")
                self.reimport_row.set_subtitle(
                    f"Re-read Tumblr cookies from {source or 'Firefox or Chromium'}"
                )
            else:
                self.reimport_row.set_subtitle(
                    "Sign in first; browser cookies are imported from the welcome page."
                )
            self.reimport_button.set_sensitive(
                signed_in and not demo and not self._reimport_busy
            )
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

    def _on_reimport(self, *_args) -> None:
        if bool(getattr(self.ctx, "demo", False)):
            self.ctx.toast("Demo mode: there is no browser session to re-import.")
            return
        if not self.ctx.is_signed_in:
            self.ctx.toast("Sign in first; re-import needs an active session.")
            return
        if self._reimport_busy:
            return
        self._reimport_busy = True
        self.reimport_button.set_sensitive(False)
        self.reimport_error_row.set_visible(False)
        self.reimport_row.set_subtitle("Looking for Tumblr cookies…")
        source = str(getattr(self.ctx.session, "source", "") or "").lower()
        session = self.ctx.session

        def work() -> None:
            result = None
            error = ""
            try:
                if "firefox" in source:
                    result = session.import_firefox()
                elif any(hint in source for hint in _CHROMIUM_HINTS):
                    result = session.import_chromium()
                else:
                    result = session.import_firefox()
                    if not (result and result.ok):
                        result = session.import_chromium()
            except Exception as exc:  # noqa: BLE001
                error = " ".join(str(exc).split()) or type(exc).__name__
            cookies = dict(session.cookies) if result is not None and result.ok else {}
            import_source = (
                getattr(result, "source", "") or getattr(session, "source", "")
                if result is not None
                else ""
            )
            message = getattr(result, "message", "") if result is not None else ""
            try:
                GLib.idle_add(
                    self._finish_reimport, bool(cookies), message, error, cookies, import_source
                )
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="settings-reimport").start()
        except Exception as exc:  # noqa: BLE001
            self._finish_reimport(False, "", str(exc), {}, "")

    def _finish_reimport(self, ok: bool, message: str, error: str,
                         cookies: dict, source: str) -> bool:
        self._reimport_busy = False
        try:
            if ok:
                self.ctx.adopt_session(cookies, source)
                self.ctx.toast(message or f"Re-imported {len(cookies)} cookies.")
                self.reimport_error_row.set_visible(False)
                try:
                    self.ctx.load_user(
                        lambda _ok: GLib.idle_add(self._on_reimport_user_loaded)
                    )
                except Exception:  # noqa: BLE001
                    pass
            else:
                detail = error or message or "No readable Tumblr cookies were found."
                self.reimport_error_row.set_subtitle(detail)
                self.reimport_error_row.set_visible(True)
                self.ctx.toast(f"Couldn't re-import the session: {detail}")
            self._update_account()
        except Exception:  # noqa: BLE001
            pass
        return False

    def _on_reimport_user_loaded(self) -> bool:
        try:
            self._render_blog_rows()
            self._refresh_blog_settings_model()
            self._load_user_settings()
        except Exception:  # noqa: BLE001
            pass
        return False

    def _on_about_clicked(self, *_args) -> None:
        try:
            window = getattr(self.ctx, "window", None)
            if window is not None:
                window.activate_action("win.about", None)
        except Exception:  # noqa: BLE001
            pass

    # -- user settings (read-only) ----------------------------------------- #
    def _build_user_settings(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("User settings")
        page.set_icon_name("preferences-system-symbolic")
        self.user_settings_page = page
        self.user_settings_group = Adw.PreferencesGroup()
        self.user_settings_group.set_title("Tumblr account")
        self.user_settings_group.set_description(
            "Read-only for now — editing these settings needs a future "
            "verified write endpoint."
        )
        page.add(self.user_settings_group)
        self.add(page)
        self._load_user_settings()

    def _add_status_row(self, group, title: str, body: str = "") -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(title)
        if body:
            row.set_subtitle(body)
        group.add(row)
        self._remember_widget(group, row)
        return row

    def _load_user_settings(self) -> None:
        self._user_settings_generation += 1
        generation = self._user_settings_generation
        self._clear_group(self.user_settings_group)
        if bool(getattr(self.ctx, "demo", False)):
            self._add_status_row(
                self.user_settings_group,
                "Not available in demo mode",
                "Account settings need a signed-in Tumblr session.",
            )
            return
        if not self.ctx.is_signed_in:
            self._add_status_row(
                self.user_settings_group,
                "Sign in to load account settings",
                "Guest mode can browse public feeds only.",
            )
            return
        self._add_status_row(
            self.user_settings_group, "Loading account settings…", ""
        )
        client = self.ctx.client

        def work() -> None:
            error = ""
            data = {}
            try:
                data = client.get_user_settings()
            except Exception as exc:  # noqa: BLE001
                error = " ".join(str(exc).split()) or "Tumblr refused the request."
            try:
                GLib.idle_add(self._apply_user_settings, generation, data, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="settings-user").start()
        except Exception as exc:  # noqa: BLE001
            self._apply_user_settings(generation, {}, str(exc))

    def _apply_user_settings(self, generation: int, data: dict, error: str) -> bool:
        if generation != self._user_settings_generation:
            return False
        self._clear_group(self.user_settings_group)
        if error:
            row = self._add_status_row(
                self.user_settings_group,
                "Couldn't load account settings",
                error,
            )
            retry = Gtk.Button(label="Retry")
            retry.add_css_class("flat")
            retry.set_valign(Gtk.Align.CENTER)
            retry.connect("clicked", lambda *_args: self._load_user_settings())
            row.add_suffix(retry)
            return False
        values = data if isinstance(data, dict) else {}
        language = values.get("language")
        email = values.get("email")
        added = 0
        for key, title in (("language", "Language"), ("email", "Email")):
            value = values.get(key)
            if isinstance(value, str) and value:
                self._add_value_row(self.user_settings_group, title, value)
                added += 1
        for key in sorted(values):
            if key in ("language", "email"):
                continue
            value = values.get(key)
            if not isinstance(value, bool):
                continue
            switch = Adw.SwitchRow()
            switch.set_title(_humanize(str(key)))
            switch.set_subtitle("Tumblr account setting (read-only)")
            switch.set_active(bool(value))
            switch.set_sensitive(False)
            self.user_settings_group.add(switch)
            self._remember_widget(self.user_settings_group, switch)
            added += 1
        if not added:
            self._add_status_row(
                self.user_settings_group,
                "No account settings were returned",
                "Tumblr answered without any displayable values.",
            )
        note = Adw.ActionRow()
        note.set_title("Read-only")
        note.set_subtitle(
            "Editing account settings needs the next write-auth phase."
        )
        note.add_css_class("dim-label")
        self.user_settings_group.add(note)
        self._remember_widget(self.user_settings_group, note)
        return False

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
            "notify::active",
            lambda *_a: self._set_pref(
                "show_avatars", self.show_avatars_row.get_active()
            ),
        )
        feed_group.add(self.show_avatars_row)

        self.compact_row = Adw.SwitchRow()
        self.compact_row.set_title("Compact layout")
        self.compact_row.set_subtitle("Tighter spacing, more posts on screen")
        self.compact_row.set_active(bool(self.ctx.prefs.get("compact", False)))
        self.compact_row.connect(
            "notify::active",
            lambda *_a: self._set_pref("compact", self.compact_row.get_active()),
        )
        feed_group.add(self.compact_row)

        self.nsfw_row = Adw.SwitchRow()
        self.nsfw_row.set_title("Blur sensitive media")
        self.nsfw_row.set_subtitle("Hide posts flagged as sensitive until you reveal them")
        self.nsfw_row.set_active(bool(self.ctx.prefs.get("nsfw_blur", True)))
        self.nsfw_row.connect(
            "notify::active",
            lambda *_a: self._set_pref("nsfw_blur", self.nsfw_row.get_active()),
        )
        feed_group.add(self.nsfw_row)

        self.autoplay_row = Adw.SwitchRow()
        self.autoplay_row.set_title("Autoplay video")
        self.autoplay_row.set_subtitle("Start videos automatically while scrolling")
        self.autoplay_row.set_active(bool(self.ctx.prefs.get("autoplay_video", False)))
        self.autoplay_row.connect(
            "notify::active",
            lambda *_a: self._set_pref("autoplay_video", self.autoplay_row.get_active()),
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
        self._set_pref("theme", value)
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
        group.set_description(
            "Images and GIF transcodes are cached on disk so scrolling stays fast."
        )

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

    # -- lifecycle --------------------------------------------------------- #
    def _on_session_changed(self, *_args) -> None:
        try:
            self._update_account()
            self._render_blog_rows()
            self._refresh_blog_settings_model()
            self._load_user_settings()
        except Exception:  # noqa: BLE001
            pass

    def _on_close_request(self, *_args) -> bool:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            self._unsubscribe = None
        return False
