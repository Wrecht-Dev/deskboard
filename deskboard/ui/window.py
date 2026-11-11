"""Main window: split navigation, toolbar, actions, shortcuts and toasts.

Owner: shell.

``MainWindow`` is the application shell.  It owns the
:class:`Adw.NavigationSplitView`, decides which page is shown in the content
pane (a feed or the sign-in page), and acts as the single place where global
events (``like``/``reblog``/``open-blog`` …) turn into work.
"""
from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import config
from ..api.sources import FeedSource
from .feed import FeedPage
from .onboarding import OnboardingPage
from .settings import SettingsWindow
from .sidebar import Sidebar

_SESSION_FEEDS = ("home", "dashboard", "likes", "following")
_KNOWN_FEEDS = ("home", "dashboard", "likes", "blog", "tagged", "search", "following")


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "DeskboardMainWindow"

    def __init__(self, app, ctx) -> None:
        super().__init__(application=app)
        self.ctx = ctx
        ctx.window = self

        self.feed_page: FeedPage | None = None
        self.current_source: FeedSource | None = None
        self.onboarding_page = None
        self._settings_window = None
        self._reblog_dialog = None
        self._about_dialog = None
        self._signout_dialog = None
        self._unsubscribes: list = []
        self._like_inflight: set[str] = set()

        self._restore_window_state()
        self._build_ui()
        self._install_actions()
        self._install_shortcuts()
        self._install_subscriptions()

        try:
            ctx.set_toast_handler(self.toast)
        except Exception:  # noqa: BLE001
            pass

        self.apply_prefs()
        try:
            self.sidebar.refresh_identity()
        except Exception:  # noqa: BLE001
            pass
        self._update_sidebar_toggle()
        self.connect("close-request", self._on_close_request)

    # -- construction ------------------------------------------------------ #
    def _restore_window_state(self) -> None:
        prefs = self.ctx.prefs
        try:
            width = int(prefs.get("window_width") or 1120)
        except (TypeError, ValueError):
            width = 1120
        try:
            height = int(prefs.get("window_height") or 820)
        except (TypeError, ValueError):
            height = 820
        width = max(760, min(width, 5000))
        height = max(560, min(height, 5000))
        # Never open larger than the screen the window will land on.
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                monitors = display.get_monitors()
                if monitors is not None and monitors.get_n_items() > 0:
                    geometry = monitors.get_item(0).get_geometry()
                    if geometry is not None and geometry.width > 0 and geometry.height > 0:
                        width = min(width, max(640, geometry.width - 64))
                        height = min(height, max(480, geometry.height - 64))
        except Exception:  # noqa: BLE001
            pass
        self.set_default_size(width, height)
        self.set_size_request(720, 520)

    def _build_ui(self) -> None:
        self.set_title(config.APP_NAME)

        # content header
        self.window_title = Adw.WindowTitle(title="Deskboard", subtitle="Tumblr")
        header = Adw.HeaderBar()
        header.set_title_widget(self.window_title)

        self.sidebar_toggle = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.sidebar_toggle.set_tooltip_text("Show sidebar")
        self.sidebar_toggle.set_visible(False)
        self.sidebar_toggle.connect("clicked", lambda *_args: self._show_sidebar())
        header.pack_start(self.sidebar_toggle)

        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_button.set_tooltip_text("Refresh (Ctrl+R)")
        self.refresh_button.connect("clicked", lambda *_args: self.refresh_current())
        header.pack_start(self.refresh_button)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Refresh", "win.refresh")
        section.append("Search", "win.search")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Preferences", "win.preferences")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("About Deskboard", "win.about")
        section.append("Quit", "app.quit")
        menu.append_section(None, section)

        self.menu_button = Gtk.MenuButton()
        self.menu_button.set_icon_name("open-menu-symbolic")
        self.menu_button.set_tooltip_text("Main menu")
        self.menu_button.set_menu_model(menu)
        header.pack_end(self.menu_button)

        self.toolbar = Adw.ToolbarView()
        self.toolbar.add_top_bar(header)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.toolbar)

        self.content_page = Adw.NavigationPage(child=self.toast_overlay, title="Deskboard")

        # sidebar
        self.sidebar = Sidebar(
            self.ctx,
            on_select=self._on_sidebar_select,
            on_search=self.open_search,
            on_sign_in=self.show_onboarding,
        )
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_title = Adw.WindowTitle(title=config.APP_NAME, subtitle=None)
        sidebar_header.set_title_widget(sidebar_title)
        sidebar_toolbar.add_top_bar(sidebar_header)
        sidebar_toolbar.set_content(self.sidebar)
        self.sidebar_page = Adw.NavigationPage(child=sidebar_toolbar, title=config.APP_NAME)

        self.split = Adw.NavigationSplitView()
        try:
            self.split.set_min_sidebar_width(250)
            self.split.set_max_sidebar_width(330)
            self.split.set_sidebar_width_fraction(0.24)
        except Exception:  # noqa: BLE001 - older runtimes
            pass
        self.split.set_sidebar(self.sidebar_page)
        self.split.set_content(self.content_page)
        self.split.connect("notify::collapsed", lambda *_args: self._update_sidebar_toggle())
        self.split.connect("notify::show-content", lambda *_args: self._update_sidebar_toggle())
        self.set_content(self.split)

        self._update_feed_header(None)

    # -- actions ----------------------------------------------------------- #
    def _install_actions(self) -> None:
        specs = (
            ("refresh", self.refresh_current),
            ("search", self.focus_search),
            ("preferences", self.open_settings),
            ("about", self._show_about),
            ("sign-out", self._confirm_sign_out),
            ("open-my-blog", self._open_my_blog),
            ("toggle-sidebar", self._show_sidebar),
        )
        for name, callback in specs:
            try:
                action = Gio.SimpleAction.new(name, None)
                action.connect("activate", lambda *_args, cb=callback: self._safe(cb))
                self.add_action(action)
            except Exception:  # noqa: BLE001
                pass

        application = self.get_application()
        if application is not None:
            for action_name, accels in (
                ("win.refresh", ["<Control>r"]),
                ("win.search", ["<Control>f"]),
                ("win.preferences", ["<Control>comma"]),
            ):
                try:
                    application.set_accels_for_action(action_name, accels)
                except Exception:  # noqa: BLE001
                    pass

    def _safe(self, callback) -> None:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - a failed action must not crash
            self.toast(f"That action failed: {exc}")

    # -- shortcuts --------------------------------------------------------- #
    def _install_shortcuts(self) -> None:
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)
        self._key_controller = controller

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        try:
            if self._is_text_focused():
                return False
            if state & Gdk.ModifierType.CONTROL_MASK:
                return False
            name = Gdk.keyval_name(keyval)
            feed = self.feed_page
            if name == "j":
                if feed is not None and feed.move_selection(1):
                    return True
            elif name == "k":
                if feed is not None and feed.move_selection(-1):
                    return True
            elif name == "o":
                if feed is not None and feed.open_selected():
                    return True
            elif name == "l":
                if feed is not None and feed.like_selected():
                    return True
            elif name == "r":
                if feed is not None and feed.reblog_selected():
                    return True
            elif name in ("/", "slash") or keyval == Gdk.KEY_slash:
                self.focus_search()
                return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _is_text_focused(self) -> bool:
        try:
            widget = self.get_focus()
            while widget is not None:
                if isinstance(widget, (Gtk.Entry, Gtk.Text, Gtk.TextView)):
                    return True
                widget = widget.get_parent()
        except Exception:  # noqa: BLE001
            return False
        return False

    # -- subscriptions ----------------------------------------------------- #
    def _install_subscriptions(self) -> None:
        events = (
            ("open-blog", self._on_open_blog),
            ("open-tag", self._on_open_tag),
            ("open-search", self._on_open_search),
            ("like", self._on_like_request),
            ("reblog", self._on_reblog_request),
            ("session-changed", self._on_session_changed),
            ("prefs-changed", self._on_prefs_changed),
        )
        for event, callback in events:
            try:
                self._unsubscribes.append(self.ctx.subscribe(event, callback))
            except Exception:  # noqa: BLE001
                pass
        try:
            self.connect("destroy", self._on_destroy)
        except Exception:  # noqa: BLE001
            pass

    def _on_destroy(self, *_args) -> None:
        for unsubscribe in list(self._unsubscribes):
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self._unsubscribes.clear()
        if self.feed_page is not None:
            try:
                self.feed_page.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self.feed_page = None

    # -- feed switching ---------------------------------------------------- #
    def open_feed(self, source) -> None:
        """Show ``source`` in the content pane (public API used by cards etc.)."""
        if source is None:
            return
        try:
            if not source.title:
                source.title = source.label
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sidebar.set_selected_source(source)
        except Exception:  # noqa: BLE001
            pass
        if self._needs_onboarding(source):
            self.show_onboarding()
            return
        self._show_feed(source)

    def open_initial(self, feed: str | None = None) -> None:
        """Open the feed requested on the command line, or the last one used."""
        source = None
        if feed:
            source = self._parse_feed(feed)
        elif self.ctx.prefs.get("last_feed"):
            stored = str(self.ctx.prefs.get("last_feed"))
            if stored == "dashboard":
                # "dashboard" was our previous default; treat it as legacy and
                # migrate once. A deliberate Following choice is stored as
                # "following" from now on, so later launches honor it.
                stored = "home"
                try:
                    self.ctx.prefs.set("last_feed", stored)
                except Exception:  # noqa: BLE001
                    pass
            source = self._parse_feed(stored)
        if source is None:
            # A stale/unknown last_feed (e.g. from an older release) migrates to
            # the Home tab instead of opening nothing.
            source = FeedSource("home", title="Home")
        if not self.ctx.demo and not self.ctx.is_signed_in:
            if source.kind in ("blog", "tagged", "search"):
                self._show_feed(source)
            else:
                self.show_onboarding()
            return
        self.open_feed(source)

    def _parse_feed(self, text: str) -> FeedSource | None:
        kind, _sep, value = str(text or "").partition(":")
        kind = kind.strip().lower()
        value = value.strip()
        if kind not in _KNOWN_FEEDS:
            return None
        if kind in ("blog", "tagged", "search") and not value:
            return None
        title = ""
        if kind == "home":
            title = "Home"
        elif kind == "following":
            # "Following" is the dashboard tab; persist it under its own name so
            # the legacy "dashboard" value can migrate to Home exactly once.
            return FeedSource("dashboard", title="Following")
        elif kind == "blog":
            title = value
        elif kind == "tagged":
            title = f"#{value}"
        elif kind == "search":
            title = f"Search: {value}"
        elif kind == "dashboard":
            title = "Following"
        return FeedSource(kind=kind, value=value, title=title)

    def _needs_onboarding(self, source) -> bool:
        if self.ctx.demo or self.ctx.is_signed_in:
            return False
        return getattr(source, "kind", "") in _SESSION_FEEDS

    def _show_feed(self, source: FeedSource) -> None:
        old_page = self.feed_page
        self.feed_page = None
        if old_page is not None:
            try:
                old_page.cleanup()
            except Exception:  # noqa: BLE001
                pass

        self.current_source = source
        self.onboarding_page = None
        page = FeedPage(
            self.ctx,
            source,
            on_progress=lambda count, total: self._on_feed_progress(source, count, total),
        )
        self.feed_page = page
        try:
            self.toolbar.set_content(page)
        except Exception:  # noqa: BLE001
            pass
        self._update_feed_header(source)
        self._persist_feed(source)
        self._update_sidebar_toggle()
        try:
            page.load_first()
        except Exception as exc:  # noqa: BLE001
            self.toast(f"Couldn't open that feed: {exc}")
        if self.split.get_collapsed():
            try:
                self.split.set_show_content(True)
            except Exception:  # noqa: BLE001
                pass

    def _feed_subtitle(self, source, count: int | None = None, total: int | None = None) -> str:
        """A short, informative second line for the header."""
        kind = getattr(source, "kind", "")
        if kind == "home":
            base = "For you"
        elif kind in ("dashboard", "following"):
            base = "Your dashboard"
        elif kind == "likes":
            base = "Posts you liked"
        elif kind == "blog":
            base = f"@{source.value}.tumblr.com"
        elif kind == "tagged":
            base = f"#{source.value}"
        elif kind == "search":
            base = f"\u201c{source.value}\u201d"
        else:
            base = "Tumblr"
        try:
            count = int(count) if count is not None else None
        except (TypeError, ValueError):
            count = None
        if not count or count <= 0:
            return base
        if total and int(total) > count:
            return f"{base} · {count} of {int(total)} posts"
        return f"{base} · {count} post" if count == 1 else f"{base} · {count} posts"

    def _on_feed_progress(self, source, count: int, total) -> None:
        try:
            if self.feed_page is None or self.current_source is not source:
                return
            self.window_title.set_subtitle(self._feed_subtitle(source, count, total))
        except Exception:  # noqa: BLE001
            pass

    def _update_feed_header(self, source: FeedSource | None) -> None:
        try:
            if source is None:
                self.window_title.set_title("Deskboard")
                self.window_title.set_subtitle("Tumblr")
                self.content_page.set_title("Deskboard")
                self.set_title(config.APP_NAME)
                return
            label = source.label
            self.window_title.set_title(label)
            self.window_title.set_subtitle(self._feed_subtitle(source))
            self.content_page.set_title(label)
            self.set_title(f"{label} — {config.APP_NAME}")
        except Exception:  # noqa: BLE001
            pass

    def _persist_feed(self, source: FeedSource) -> None:
        try:
            # Store Following explicitly (not the legacy "dashboard" value), so
            # the one-time dashboard -> Home migration cannot override a user
            # who deliberately picks Following.
            value = "following" if source.kind == "dashboard" else source.key
            self.ctx.prefs.set("last_feed", value)
        except Exception:  # noqa: BLE001
            pass

    def _on_sidebar_select(self, source) -> None:
        self.open_feed(source)

    def show_onboarding(self, *_args) -> None:
        """Swap the content pane to the sign-in page."""
        old_page = self.feed_page
        self.feed_page = None
        if old_page is not None:
            try:
                old_page.cleanup()
            except Exception:  # noqa: BLE001
                pass
        self.current_source = None
        if self.onboarding_page is not None:
            try:
                self.toolbar.set_content(self.onboarding_page)
                self._update_feed_header(None)
                self.window_title.set_title("Welcome to Deskboard")
                self.window_title.set_subtitle("Sign in with the session from your browser")
                if self.split.get_collapsed():
                    self.split.set_show_content(True)
                return
            except Exception:  # noqa: BLE001
                pass
        page = OnboardingPage(
            self.ctx,
            on_ready=self._on_signed_in,
            on_browse=self._on_browse_anonymously,
        )
        self.onboarding_page = page
        try:
            self.toolbar.set_content(page)
        except Exception:  # noqa: BLE001
            pass
        self._update_feed_header(None)
        try:
            self.window_title.set_title("Welcome to Deskboard")
            self.window_title.set_subtitle("Sign in with the session from your browser")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sidebar.select("")
        except Exception:  # noqa: BLE001
            pass
        if self.split.get_collapsed():
            try:
                self.split.set_show_content(True)
            except Exception:  # noqa: BLE001
                pass

    def _on_browse_anonymously(self) -> None:
        self.open_feed(FeedSource("blog", value="staff", title="Staff picks"))

    def _on_signed_in(self) -> None:
        try:
            self.sidebar.refresh_identity()
        except Exception:  # noqa: BLE001
            pass
        self.open_feed(FeedSource("home", title="Home"))

    # -- search ------------------------------------------------------------ #
    def focus_search(self) -> None:
        try:
            if self.split.get_collapsed():
                self.split.set_show_content(False)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sidebar.focus_search()
        except Exception:  # noqa: BLE001
            pass

    def open_search(self, query: str) -> None:
        query = str(query or "").strip()
        if not query:
            self.focus_search()
            return
        source = FeedSource("search", value=query, title=f"Search: {query}")
        self.open_feed(source)
        if self.feed_page is not None:
            self.feed_page.focus_list()

    def _on_open_search(self, query) -> None:
        self.open_search(str(query or ""))

    # -- feed actions ------------------------------------------------------ #
    def refresh_current(self) -> None:
        if self.feed_page is not None:
            try:
                self.feed_page.refresh()
            except Exception as exc:  # noqa: BLE001
                self.toast(f"Couldn't refresh: {exc}")
            return
        if self.current_source is not None:
            self.open_feed(self.current_source)
        elif not self.ctx.demo and not self.ctx.is_signed_in:
            self.show_onboarding()
        else:
            self.open_feed(FeedSource("home", title="Home"))

    def _on_open_blog(self, name) -> None:
        blog = str(name or "").strip()
        if not blog:
            return
        self.open_feed(FeedSource("blog", value=blog, title=blog))

    def _on_open_tag(self, tag) -> None:
        tag = str(tag or "").strip().lstrip("#")
        if not tag:
            return
        self.open_feed(FeedSource("tagged", value=tag, title=f"#{tag}"))

    def _open_my_blog(self) -> None:
        blog = str(getattr(self.ctx, "user_blog", "") or "")
        if not blog and getattr(self.ctx, "user_blogs", None):
            blog = str(self.ctx.user_blogs[0])
        if not blog:
            self.toast("Sign in to open your blog.")
            self.show_onboarding()
            return
        self.open_feed(FeedSource("blog", value=blog, title=blog))

    # -- liking ------------------------------------------------------------ #
    def _can_write(self) -> bool:
        return bool(getattr(self.ctx, "demo", False)) or bool(self.ctx.is_signed_in)

    def _on_like_request(self, post) -> None:
        if post is None:
            return
        if not self._can_write():
            self.toast("Sign in to like posts.")
            return
        key = str(getattr(post, "id", "") or id(post))
        if key in self._like_inflight:
            return
        self._like_inflight.add(key)
        # Cards (and our keyboard shortcut) flip the post optimistically before
        # publishing, so the current flag is the state the user wants.
        desired = bool(getattr(post, "is_liked", False))
        client = self.ctx.client

        def work() -> None:
            error = ""
            try:
                if desired:
                    client.like(post)
                else:
                    client.unlike(post)
            except Exception as exc:  # noqa: BLE001
                error = " ".join(str(exc).split()) or "Tumblr refused the request."
            try:
                GLib.idle_add(self._finish_like, post, key, desired, error)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="like").start()
        except Exception as exc:  # noqa: BLE001
            self._like_inflight.discard(key)
            self._finish_like(post, key, desired, str(exc))

    def _finish_like(self, post, key: str, desired: bool, error: str) -> bool:
        self._like_inflight.discard(key)
        try:
            if error:
                # Roll the optimistic toggle back so the UI never lies.
                try:
                    post.is_liked = not desired
                    delta = 1 if desired else -1
                    post.like_count = max(0, int(getattr(post, "like_count", 0) or 0) - delta)
                except Exception:  # noqa: BLE001
                    pass
                self.ctx.publish("post-changed", post)
                self.toast(f"Couldn't update the like: {error}")
                return False
            post.is_liked = desired
            try:
                count = int(getattr(post, "note_count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            post.note_count = max(0, count + (1 if desired else -1))
            self.ctx.publish("post-changed", post)
            self.toast("Liked" if desired else "Removed from likes")
        except Exception:  # noqa: BLE001
            pass
        return False

    # -- reblog ------------------------------------------------------------ #
    def _on_reblog_request(self, post) -> None:
        if post is None:
            return
        self.open_reblog_dialog(post)

    def open_reblog_dialog(self, post) -> None:
        if not self._can_write():
            self.toast("Sign in to reblog posts.")
            return
        blog = str(getattr(self.ctx, "user_blog", "") or "")
        if not blog and getattr(self.ctx, "user_blogs", None):
            blog = str(self.ctx.user_blogs[0])
        if not blog:
            self.toast("No blog is available to reblog to.")
            return
        if self._reblog_dialog is not None:
            try:
                self._reblog_dialog.close()
            except Exception:  # noqa: BLE001
                pass
            self._reblog_dialog = None

        try:
            dialog = Adw.Dialog()
            dialog.set_title("Reblog")
            try:
                dialog.set_content_width(520)
                dialog.set_content_height(400)
            except Exception:  # noqa: BLE001
                pass

            toolbar = Adw.ToolbarView()
            header = Adw.HeaderBar()
            try:
                header.set_show_end_title_buttons(False)
            except Exception:  # noqa: BLE001
                pass
            header.set_title_widget(
                Adw.WindowTitle(title="Reblog", subtitle=f"to {blog}")
            )
            cancel_button = Gtk.Button(label="Cancel")
            cancel_button.add_css_class("flat")
            cancel_button.connect("clicked", lambda *_args: dialog.close())
            header.pack_start(cancel_button)
            reblog_button = Gtk.Button(label="Reblog")
            reblog_button.add_css_class("suggested-action")
            header.pack_end(reblog_button)
            toolbar.add_top_bar(header)

            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            body.set_margin_top(14)
            body.set_margin_bottom(16)
            body.set_margin_start(16)
            body.set_margin_end(16)
            caption = Gtk.Label(label="Add a comment", xalign=0)
            caption.add_css_class("caption")
            body.append(caption)

            text_view = Gtk.TextView()
            text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            text_view.set_top_margin(10)
            text_view.set_bottom_margin(10)
            text_view.set_left_margin(10)
            text_view.set_right_margin(10)
            text_view.set_hexpand(True)
            text_view.set_vexpand(True)
            buffer = text_view.get_buffer()
            frame = Gtk.Frame()
            frame.set_child(text_view)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_child(frame)
            scroller.set_min_content_height(180)
            scroller.set_vexpand(True)
            body.append(scroller)
            toolbar.set_content(body)
            dialog.set_child(toolbar)

            def on_reblog(*_args) -> None:
                try:
                    comment = buffer.get_text(
                        buffer.get_start_iter(), buffer.get_end_iter(), True
                    ).strip()
                except Exception:  # noqa: BLE001
                    comment = ""
                reblog_button.set_sensitive(False)
                cancel_button.set_sensitive(False)
                reblog_button.set_label("Reblogging…")

                def apply(error: str) -> bool:
                    try:
                        if error:
                            reblog_button.set_label("Reblog")
                            reblog_button.set_sensitive(True)
                            cancel_button.set_sensitive(True)
                            self.toast(f"Couldn't reblog: {error}")
                        else:
                            dialog.close()
                            self.toast(f"Reblogged to {blog}")
                    except Exception:  # noqa: BLE001
                        pass
                    return False

                def work() -> None:
                    error = ""
                    try:
                        self.ctx.client.reblog(post, blog, comment)
                    except Exception as exc:  # noqa: BLE001
                        error = " ".join(str(exc).split()) or "Tumblr refused the request."
                    try:
                        GLib.idle_add(apply, error)
                    except Exception:  # noqa: BLE001
                        pass

                try:
                    threading.Thread(target=work, daemon=True, name="reblog").start()
                except Exception as exc:  # noqa: BLE001
                    apply(str(exc))

            reblog_button.connect("clicked", on_reblog)

            def on_closed(_dialog) -> None:
                self._reblog_dialog = None

            dialog.connect("closed", on_closed)
            self._reblog_dialog = dialog
            dialog.present(self)
        except Exception as exc:  # noqa: BLE001
            self._reblog_dialog = None
            self.toast(f"Couldn't open the reblog dialog: {exc}")

    # -- session ----------------------------------------------------------- #
    def _on_session_changed(self, signed_in) -> None:
        try:
            self.sidebar.refresh_identity()
        except Exception:  # noqa: BLE001
            pass
        if not signed_in and not self.ctx.demo:
            source = self.current_source
            if source is not None and getattr(source, "kind", "") in _SESSION_FEEDS:
                self.show_onboarding()

    def _confirm_sign_out(self) -> None:
        if getattr(self.ctx, "demo", False):
            self.toast("Demo mode: there is no real session to sign out of.")
            return
        if not self.ctx.is_signed_in:
            self.toast("You are already browsing as a guest.")
            return
        try:
            dialog = Adw.AlertDialog()
            dialog.set_heading("Sign out of Tumblr?")
            dialog.set_body(
                "Deskboard will forget the imported session cookies on this computer. "
                "Public blogs, tags and search keep working."
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("sign-out", "Sign out")
            dialog.set_response_appearance("sign-out", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_response(_dialog, response: str) -> None:
                self._signout_dialog = None
                if response == "sign-out":
                    try:
                        self.ctx.sign_out()
                    except Exception as exc:  # noqa: BLE001
                        self.toast(f"Couldn't sign out: {exc}")
                        return
                    self.toast("Signed out")

            dialog.connect("response", on_response)
            self._signout_dialog = dialog
            dialog.present(self)
        except Exception as exc:  # noqa: BLE001
            self.toast(f"Couldn't sign out: {exc}")

    # -- preferences ------------------------------------------------------- #
    def apply_prefs(self) -> None:
        try:
            if bool(self.ctx.prefs.get("compact")):
                self.add_css_class("compact")
            else:
                self.remove_css_class("compact")
        except Exception:  # noqa: BLE001
            pass

    def _on_prefs_changed(self, key: str = "") -> None:
        if key in ("", "compact"):
            self.apply_prefs()

    def open_settings(self) -> None:
        try:
            if self._settings_window is not None:
                self._settings_window.present()
                return
            window = SettingsWindow(self.ctx)
            window.set_transient_for(self)
            self._settings_window = window

            def on_destroy(*_args) -> None:
                self._settings_window = None

            window.connect("destroy", on_destroy)
            window.present()
        except Exception as exc:  # noqa: BLE001
            self.toast(f"Couldn't open preferences: {exc}")

    # -- sidebar / window state -------------------------------------------- #
    def _show_sidebar(self) -> None:
        try:
            if self.split.get_collapsed():
                self.split.set_show_content(False)
        except Exception:  # noqa: BLE001
            pass
        self._update_sidebar_toggle()

    def _update_sidebar_toggle(self) -> None:
        try:
            collapsed = bool(self.split.get_collapsed())
            showing_content = bool(self.split.get_show_content())
            self.sidebar_toggle.set_visible(collapsed and showing_content)
        except Exception:  # noqa: BLE001
            pass

    def apply_startup_state(self) -> None:
        try:
            if bool(self.ctx.prefs.get("window_maximized")):
                self.maximize()
        except Exception:  # noqa: BLE001
            pass

    def save_state(self) -> None:
        """Persist geometry/maximized state (used on close and on quit)."""
        self._save_window_state()

    def _on_close_request(self, *_args) -> bool:
        self._save_window_state()
        if self._settings_window is not None:
            try:
                self._settings_window.close()
            except Exception:  # noqa: BLE001
                pass
        if self.feed_page is not None:
            try:
                self.feed_page.cleanup()
            except Exception:  # noqa: BLE001
                pass
        return False

    def _save_window_state(self) -> None:
        try:
            prefs = self.ctx.prefs
            maximized = bool(self.is_maximized())
            if not maximized and not self.is_fullscreen():
                width = int(self.get_width() or 0)
                height = int(self.get_height() or 0)
                if width >= 400 and height >= 300:
                    prefs.set("window_width", width)
                    prefs.set("window_height", height)
            prefs.set("window_maximized", maximized)
        except Exception:  # noqa: BLE001
            pass

    # -- public helpers ---------------------------------------------------- #
    def toast(self, message: str) -> None:
        """Show a transient message at the bottom of the content pane."""
        text = str(message or "").strip()
        if not text:
            return
        try:
            toast = Adw.Toast.new(text)
            toast.set_timeout(4)
            self.toast_overlay.add_toast(toast)
        except Exception:  # noqa: BLE001
            pass

    def on_user_loaded(self, ok: bool) -> None:
        """Called by the application once the signed-in account is known."""
        try:
            self.sidebar.refresh_identity()
        except Exception:  # noqa: BLE001
            pass

    def _show_about(self) -> None:
        try:
            if self._about_dialog is not None:
                self._about_dialog.present(self)
                return
            dialog = Adw.AboutDialog()
            dialog.set_application_name(config.APP_NAME)
            dialog.set_application_icon(config.APP_ID)
            dialog.set_version(config.APP_VERSION)
            dialog.set_comments(config.APP_TAGLINE)
            dialog.set_developer_name("Deskboard contributors")
            try:
                dialog.set_license_type(Gtk.License.MIT_X11)
            except Exception:  # noqa: BLE001
                pass
            self._about_dialog = dialog

            def on_closed(_dialog) -> None:
                self._about_dialog = None

            dialog.connect("closed", on_closed)
            dialog.present(self)
        except Exception as exc:  # noqa: BLE001
            self._about_dialog = None
            self.toast(f"Couldn't open the about dialog: {exc}")
