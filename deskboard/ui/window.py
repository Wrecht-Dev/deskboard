"""Main window: split navigation, toolbar, actions, shortcuts and toasts.

Owner: shell.

``MainWindow`` is the application shell.  It owns the
:class:`Adw.NavigationSplitView`, decides which page is shown in the content
pane (a feed or the sign-in page), and acts as the single place where global
events (``like``/``reblog``/``open-blog`` …) turn into work.
"""
from __future__ import annotations

import datetime
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
        self.page_widget = None
        self.current_source: FeedSource | None = None
        self._last_feed_source: FeedSource | None = None
        self._page_return_source: FeedSource | None = None
        self.onboarding_page = None
        self._settings_window = None
        self._reblog_dialog = None
        self._about_dialog = None
        self._signout_dialog = None
        self._composer = None
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

        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.back_button.set_tooltip_text("Back")
        self.back_button.set_visible(False)
        self.back_button.connect("clicked", lambda *_args: self._back_from_page())
        header.pack_start(self.back_button)

        self.sidebar_toggle = Gtk.Button.new_from_icon_name("sidebar-show-symbolic")
        self.sidebar_toggle.set_tooltip_text("Show sidebar")
        self.sidebar_toggle.set_visible(False)
        self.sidebar_toggle.connect("clicked", lambda *_args: self._show_sidebar())
        header.pack_start(self.sidebar_toggle)

        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_button.set_tooltip_text("Refresh (Ctrl+R)")
        self.refresh_button.connect("clicked", lambda *_args: self.refresh_current())
        header.pack_start(self.refresh_button)

        self.compose_button = Gtk.Button.new_from_icon_name("document-new-symbolic")
        self.compose_button.set_tooltip_text("New post (Ctrl+N)")
        self.compose_button.connect("clicked", lambda *_args: self.open_composer())
        header.pack_end(self.compose_button)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("New post", "win.compose")
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
            on_page=self._open_page,
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
            ("compose", self.open_composer),
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
                ("win.compose", ["<Control>n"]),
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
        if self.page_widget is not None:
            try:
                self._discard_page(self.page_widget)
            except Exception:  # noqa: BLE001
                pass
            self.page_widget = None

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
        kind = getattr(source, "kind", "")
        if kind == "explore":
            try:
                from .explore import ExplorePage

                page = ExplorePage(self.ctx)
                self._persist_feed(source)
                self.show_page(page, "Explore", "Recommended")
                try:
                    self.sidebar.select(source.key)
                except Exception:  # noqa: BLE001
                    pass
                return
            except Exception:  # noqa: BLE001 - fall back to the bare feed
                pass
        if kind == "blog" and getattr(source, "value", ""):
            try:
                from .blogpage import BlogPage

                page = BlogPage(self.ctx, source.value)
                title = source.title or source.value
                self._persist_feed(source)
                self.show_page(page, title, f"@{source.value}.tumblr.com")
                try:
                    self.sidebar.select(source.key)
                except Exception:  # noqa: BLE001
                    pass
                return
            except Exception:  # noqa: BLE001 - fall back to the bare feed
                pass
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
        self._clear_page_widget()
        old_page = self.feed_page
        self.feed_page = None
        if old_page is not None:
            try:
                old_page.cleanup()
            except Exception:  # noqa: BLE001
                pass

        self._last_feed_source = source
        self._page_return_source = None
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

    # -- generic page host ------------------------------------------------ #
    def show_page(self, widget, title: str, subtitle: str | None = None) -> None:
        """Show an arbitrary page in the content pane.

        The header (refresh/menu/toasts) stays; back is offered when the page
        sets a truthy ``on_back`` (callable callbacks are invoked first).  Pages
        may implement ``refresh()`` for Ctrl+R and ``cleanup()`` for teardown.
        """
        if widget is None:
            return
        if self._page_return_source is None and self._last_feed_source is not None:
            self._page_return_source = self._last_feed_source
        old_page = self.page_widget
        if old_page is not None and old_page is not widget:
            self._discard_page(old_page)
        old_feed = self.feed_page
        self.feed_page = None
        if old_feed is not None:
            try:
                old_feed.cleanup()
            except Exception:  # noqa: BLE001
                pass
        self.page_widget = widget
        self.current_source = None
        self.onboarding_page = None
        try:
            self.toolbar.set_content(widget)
        except Exception:  # noqa: BLE001
            pass
        label = str(title or "Deskboard")
        try:
            self.window_title.set_title(label)
            self.window_title.set_subtitle(str(subtitle or ""))
            self.content_page.set_title(label)
            self.set_title(f"{label} — {config.APP_NAME}")
        except Exception:  # noqa: BLE001
            pass
        self._update_back_button()
        try:
            if self.split.get_collapsed():
                self.split.set_show_content(True)
        except Exception:  # noqa: BLE001
            pass
        self._update_sidebar_toggle()

    def go_back(self) -> None:
        """Public alias for the header back button."""
        self._back_from_page()

    def _back_from_page(self) -> None:
        widget = self.page_widget
        if widget is None:
            return
        callback = getattr(widget, "on_back", None)
        if callable(callback):
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass
        if self.page_widget is widget:
            self._restore_previous_view()

    def _restore_previous_view(self) -> None:
        """Return from a hosted page to the feed the user came from."""
        source = self._page_return_source or self._last_feed_source
        self.page_widget = None
        self._page_return_source = None
        self._update_back_button()
        if source is not None:
            self.open_feed(source)
        else:
            self.open_feed(FeedSource("home", title="Home"))

    def _clear_page_widget(self) -> None:
        page = self.page_widget
        self.page_widget = None
        self._update_back_button()
        if page is not None:
            self._discard_page(page)

    @staticmethod
    def _discard_page(page) -> None:
        cleanup = getattr(page, "cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception:  # noqa: BLE001
                pass

    def _update_back_button(self) -> None:
        try:
            widget = self.page_widget
            self.back_button.set_visible(
                widget is not None and getattr(widget, "on_back", None) is not None
            )
        except Exception:  # noqa: BLE001
            pass

    def _open_page(self, key: str) -> None:
        """Lazily construct a non-feed page requested by the sidebar."""
        key = str(key or "")
        titles = {
            "notifications": ("Notifications", "Activity"),
            "messages": ("Messages", "Conversations"),
            "inbox": ("Inbox", "Asks & submissions"),
            "drafts": ("Drafts", "Saved posts"),
            "queue": ("Queue", "Scheduled posts"),
        }
        title, subtitle = titles.get(key, (key.title() or "Page", ""))
        page = None
        try:
            if key == "notifications":
                from .activity import NotificationsPage

                page = NotificationsPage(self.ctx)
            elif key == "messages":
                from .messaging import MessagingPage

                page = MessagingPage(self.ctx)
            elif key in ("inbox", "drafts", "queue"):
                from .library import DraftsPage, InboxPage, QueuePage

                blog = str(getattr(self.ctx, "user_blog", "") or "")
                if not blog and getattr(self.ctx, "user_blogs", None):
                    blog = str(self.ctx.user_blogs[0])
                if key == "inbox":
                    page = InboxPage(self.ctx)
                elif key == "drafts":
                    page = DraftsPage(self.ctx, blog)
                else:
                    page = QueuePage(self.ctx, blog)
        except Exception as exc:  # noqa: BLE001 - modules/constructors may land later
            page = self._placeholder_page(title, exc)
        if page is None:
            page = self._placeholder_page(title, None)
        if getattr(page, "on_back", None) is None:
            page.on_back = True
        self.show_page(page, title, subtitle)
        try:
            self.sidebar.select(key)
        except Exception:  # noqa: BLE001
            pass

    def _placeholder_page(self, title: str, error: BaseException | None):
        page = Adw.StatusPage()
        page.set_icon_name("view-grid-symbolic")
        page.set_title(title)
        if error is None:
            page.set_description("This view is coming in a later update.")
        else:
            page.set_description(f"This view is not available yet ({error}).")
        page.set_vexpand(True)
        page.on_back = True
        return page

    # -- composer ---------------------------------------------------------- #
    def open_composer(self, attachments=None) -> None:
        """Open the writing window, optionally with uploaded media."""
        if not getattr(self.ctx, "demo", False) and not self.ctx.is_signed_in:
            self.toast("Sign in to write posts.")
            self.show_onboarding()
            return
        blogs = [str(name) for name in (getattr(self.ctx, "user_blogs", None) or []) if name]
        if not blogs and getattr(self.ctx, "user_blog", ""):
            blogs = [str(self.ctx.user_blog)]
        if not blogs:
            user = str(getattr(self.ctx, "user_name", "") or "")
            if user:
                blogs = [user]
        if not blogs:
            if self.ctx.is_signed_in:
                self.toast("Loading your blogs…")
                try:
                    self.ctx.load_user(
                        lambda _ok: GLib.idle_add(
                            self._open_composer_after_user, attachments
                        )
                    )
                    return
                except Exception:  # noqa: BLE001
                    pass
            self.toast("No blog is available to post to.")
            return
        self._open_composer_after_user(attachments)

    def _open_composer_after_user(self, attachments=None) -> bool:
        if self._composer is not None:
            try:
                for media in attachments or []:
                    self._composer.add_media(media)
                self._composer.present()
            except Exception:  # noqa: BLE001
                pass
            return False
        try:
            from .composer import ComposerWindow

            composer = ComposerWindow(
                self.ctx,
                parent=self,
                attachments=attachments or [],
                on_done=self._on_composer_done,
                on_closed=lambda: setattr(self, "_composer", None),
            )
            self._composer = composer
            composer.present()
        except Exception as exc:  # noqa: BLE001
            self._composer = None
            self.toast(f"Couldn't open the composer: {exc}")
        return False

    def add_composer_media(self, media) -> None:
        """Forward one uploaded media dict to the open composer."""
        if self._composer is not None:
            try:
                self._composer.add_media(media)
                self._composer.present()
                return
            except Exception:  # noqa: BLE001
                pass
        self.open_composer(attachments=[media] if media else [])

    def _on_composer_done(self, state: str, result) -> None:
        try:
            page = self.page_widget
            if page is None:
                return
            name = type(page).__name__
            if (state == "draft" and name == "DraftsPage") or (
                state == "queue" and name == "QueuePage"
            ):
                refresh = getattr(page, "refresh", None)
                if callable(refresh):
                    refresh()
        except Exception:  # noqa: BLE001
            pass

    def open_post(self, post) -> None:
        """Open a post permalink page when the detail module is available."""
        if post is None:
            return
        try:
            from .postdetail import PostDetailPage

            page = PostDetailPage(self.ctx, post, on_back=self._restore_previous_view)
            handle = str(getattr(post, "handle", "") or "")
            subtitle = f"@{handle}" if handle else ""
            self.show_page(page, post.display_name or "Post", subtitle)
            return
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.toast(f"Couldn't open the post: {exc}")
        url = str(getattr(post, "post_url", "") or getattr(post, "blog_url", "") or "")
        if url:
            self.ctx.open_url(url)

    def _feed_subtitle(self, source, count: int | None = None, total: int | None = None) -> str:
        """A short, informative second line for the header."""
        kind = getattr(source, "kind", "")
        if kind == "home":
            base = "For you"
        elif kind == "explore":
            base = "Recommended"
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
        self._clear_page_widget()
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
        try:
            from .search import SearchPage

            page = SearchPage(self.ctx, query)
            self.show_page(page, f"Search: {query}", f"\u201c{query}\u201d")
            if callable(getattr(page, "focus_posts", None)):
                page.focus_posts()
            return
        except Exception:  # noqa: BLE001 - fall back to the plain post feed
            pass
        source = FeedSource("search", value=query, title=f"Search: {query}")
        self.open_feed(source)
        if self.feed_page is not None:
            self.feed_page.focus_list()

    def _on_open_search(self, query) -> None:
        self.open_search(str(query or ""))

    # -- feed actions ------------------------------------------------------ #
    def refresh_current(self) -> None:
        page = self.page_widget
        if page is not None:
            refresh = getattr(page, "refresh", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as exc:  # noqa: BLE001
                    self.toast(f"Couldn't refresh: {exc}")
            return
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
        try:
            from .blogpage import BlogPage

            page = BlogPage(self.ctx, blog)
            self.show_page(page, blog, f"@{blog}.tumblr.com")
            return
        except Exception:  # noqa: BLE001 - fall back to the plain blog feed
            pass
        self._on_open_blog(blog)

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
        self._on_open_blog(blog)

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
            if getattr(self.ctx, "demo", False):
                self.toast("Demo mode: like state is not saved.")
            else:
                self.toast("Liked" if desired else "Removed from likes")
        except Exception:  # noqa: BLE001
            pass
        return False

    # -- reblog ------------------------------------------------------------ #
    def _on_reblog_request(self, post) -> None:
        if post is None:
            return
        self.open_reblog_dialog(post)

    def _reblog_write(self, post, blog: str, comment: str = "", tags: str = "",
                      publish_on: str | None = None) -> None:
        """Call the client's reblog endpoint, tolerating older clients."""
        client = self.ctx.client
        params = {}
        try:
            import inspect

            params = inspect.signature(client.reblog).parameters
        except Exception:  # noqa: BLE001
            params = {}
        supports_tags = "tags" in params
        supports_publish = "publish_on" in params
        if publish_on and not supports_publish:
            raise RuntimeError("This client cannot schedule reblogs.")
        kwargs = {}
        if supports_tags:
            kwargs["tags"] = tags
        if supports_publish and publish_on:
            kwargs["publish_on"] = publish_on
        if supports_tags:
            client.reblog(post, blog, comment, **kwargs)
        else:
            client.reblog(post, blog, comment)

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

        demo = bool(getattr(self.ctx, "demo", False))
        schedule_state = {"iso": None, "display": ""}
        try:
            dialog = Adw.Dialog()
            dialog.set_title("Reblog")
            try:
                dialog.set_content_width(560)
                dialog.set_content_height(580)
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
            toolbar.add_top_bar(header)

            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            body.set_margin_top(14)
            body.set_margin_bottom(10)
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
            scroller.set_min_content_height(145)
            scroller.set_vexpand(True)
            body.append(scroller)

            now_note = Gtk.Label(
                label="Reblog now sends only the post; your text won't be added."
            )
            now_note.set_wrap(True)
            now_note.set_xalign(0)
            now_note.add_css_class("caption")
            now_note.add_css_class("dim-label")
            now_note.set_visible(False)
            body.append(now_note)

            tags_caption = Gtk.Label(label="Tags (comma separated)", xalign=0)
            tags_caption.add_css_class("caption")
            body.append(tags_caption)
            tags_entry = Gtk.Entry()
            tags_entry.set_placeholder_text("art, photography, design")
            tags_entry.set_hexpand(True)
            body.append(tags_entry)

            error_label = Gtk.Label(label="")
            error_label.set_wrap(True)
            error_label.set_xalign(0)
            error_label.add_css_class("composer-error")
            error_label.set_visible(False)
            body.append(error_label)

            demo_notice = Gtk.Label(
                label="Demo mode: reblogging is disabled. Nothing will be posted."
            )
            demo_notice.set_wrap(True)
            demo_notice.set_xalign(0)
            demo_notice.add_css_class("composer-notice")
            demo_notice.set_visible(demo)
            body.append(demo_notice)

            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            bar.add_css_class("composer-actionbar")

            schedule_menu = Gtk.MenuButton()
            schedule_menu.set_icon_name("alarm-symbolic")
            schedule_menu.set_tooltip_text("Choose a schedule time")
            schedule_menu.add_css_class("flat")
            bar.append(schedule_menu)
            schedule_label = Gtk.Label(label="Choose time")
            schedule_label.add_css_class("caption")
            schedule_label.add_css_class("dim-label")
            bar.append(schedule_label)
            spinner = Adw.Spinner()
            spinner.set_size_request(18, 18)
            spinner.set_visible(False)
            bar.append(spinner)

            spacer = Gtk.Box()
            spacer.set_hexpand(True)
            bar.append(spacer)

            now_button = Gtk.Button(label="Reblog now")
            now_button.set_tooltip_text("Reblog the post without your commentary")
            bar.append(now_button)
            schedule_action = Gtk.Button(label="Schedule")
            schedule_action.set_tooltip_text("Schedule this reblog for the chosen time")
            schedule_action.set_sensitive(False)
            bar.append(schedule_action)
            reblog_button = Gtk.Button(label="Reblog")
            reblog_button.add_css_class("suggested-action")
            bar.append(reblog_button)
            toolbar.add_bottom_bar(bar)

            popover = Gtk.Popover()
            pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            pop_box.set_margin_start(12)
            pop_box.set_margin_end(12)
            pop_box.set_margin_top(12)
            pop_box.set_margin_bottom(12)
            calendar = Gtk.Calendar()
            pop_box.append(calendar)
            now = datetime.datetime.now()
            time_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            time_caption = Gtk.Label(label="Time")
            time_caption.add_css_class("caption")
            time_caption.add_css_class("dim-label")
            time_row.append(time_caption)
            hour_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
            hour_spin.set_value(min(23, now.hour + 1))
            hour_spin.set_digits(0)
            time_row.append(hour_spin)
            time_row.append(Gtk.Label(label=":"))
            minute_spin = Gtk.SpinButton.new_with_range(0, 59, 5)
            minute_spin.set_value((now.minute // 5) * 5)
            minute_spin.set_digits(0)
            time_row.append(minute_spin)
            pop_box.append(time_row)
            pop_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            clear_button = Gtk.Button(label="Clear")
            clear_button.add_css_class("flat")
            pop_buttons.append(clear_button)
            pop_spacer = Gtk.Box()
            pop_spacer.set_hexpand(True)
            pop_buttons.append(pop_spacer)
            set_button = Gtk.Button(label="Set time")
            set_button.add_css_class("suggested-action")
            pop_buttons.append(set_button)
            pop_box.append(pop_buttons)
            popover.set_child(pop_box)
            schedule_menu.set_popover(popover)

            def clear_schedule() -> None:
                schedule_state["iso"] = None
                schedule_state["display"] = ""
                schedule_label.set_text("Choose time")
                schedule_action.set_sensitive(False)
                error_label.set_visible(False)
                try:
                    popover.popdown()
                except Exception:  # noqa: BLE001
                    pass

            def apply_schedule() -> None:
                try:
                    date = calendar.get_date()
                    local = datetime.datetime(
                        int(date.get_year()),
                        int(date.get_month()),
                        int(date.get_day_of_month()),
                        int(hour_spin.get_value()),
                        int(minute_spin.get_value()),
                    ).astimezone()
                    if local <= datetime.datetime.now().astimezone():
                        error_label.set_text("Choose a time in the future.")
                        error_label.set_visible(True)
                        return
                    utc = local.astimezone(datetime.timezone.utc)
                    schedule_state["iso"] = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    schedule_state["display"] = local.strftime("%b %d, %H:%M")
                    schedule_label.set_text(local.strftime("%b %d, %H:%M"))
                    schedule_action.set_sensitive(not demo)
                    error_label.set_visible(False)
                    popover.popdown()
                except Exception as exc:  # noqa: BLE001
                    error_label.set_text(f"Could not set that time: {exc}")
                    error_label.set_visible(True)

            clear_button.connect("clicked", lambda *_args: clear_schedule())
            set_button.connect("clicked", lambda *_args: apply_schedule())

            toolbar.set_content(body)
            dialog.set_child(toolbar)

            def comment_text() -> str:
                try:
                    return buffer.get_text(
                        buffer.get_start_iter(), buffer.get_end_iter(), True
                    ).strip()
                except Exception:  # noqa: BLE001
                    return ""

            def on_comment_changed(_buffer) -> None:
                try:
                    now_note.set_visible(bool(comment_text()))
                except Exception:  # noqa: BLE001
                    pass

            buffer.connect("changed", on_comment_changed)

            def set_busy(busy: bool, status: str = "") -> None:
                for button in (now_button, reblog_button):
                    button.set_sensitive(not busy and not demo)
                schedule_action.set_sensitive(
                    not busy and not demo and bool(schedule_state["iso"])
                )
                schedule_menu.set_sensitive(not busy and not demo)
                cancel_button.set_sensitive(not busy)
                spinner.set_visible(busy)
                if status:
                    schedule_label.set_text(status)
                elif schedule_state["display"]:
                    schedule_label.set_text(schedule_state["display"])
                else:
                    schedule_label.set_text("Choose time")
                if busy:
                    error_label.set_visible(False)

            def submit(mode: str) -> None:
                if demo:
                    error_label.set_text("Demo mode: reblogging is disabled.")
                    error_label.set_visible(True)
                    self.toast("Demo mode: reblogging is disabled.")
                    return
                comment = comment_text() if mode != "now" else ""
                tags = ""
                try:
                    tags = tags_entry.get_text().strip()
                except Exception:  # noqa: BLE001
                    tags = ""
                publish_on = None
                if mode == "schedule":
                    publish_on = schedule_state["iso"]
                    if not publish_on:
                        error_label.set_text("Choose a schedule time first.")
                        error_label.set_visible(True)
                        return
                    display = schedule_state["display"]
                else:
                    display = ""
                error_label.set_visible(False)
                set_busy(True, "Scheduling…" if mode == "schedule" else "Reblogging…")

                def apply(error: str) -> bool:
                    try:
                        if error:
                            set_busy(False)
                            error_label.set_text(f"Couldn't reblog: {error}")
                            error_label.set_visible(True)
                            self.toast(f"Couldn't reblog: {error}")
                        else:
                            dialog.close()
                            if mode == "now":
                                self.toast(f"Reblogged now to {blog}")
                            elif mode == "schedule":
                                self.toast(f"Scheduled for {display} — added to queue")
                            else:
                                self.toast(f"Reblogged to {blog}")
                    except Exception:  # noqa: BLE001
                        pass
                    return False

                def work() -> None:
                    error = ""
                    try:
                        self._reblog_write(post, blog, comment, tags, publish_on)
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

            now_button.connect("clicked", lambda *_args: submit("now"))
            schedule_action.connect("clicked", lambda *_args: submit("schedule"))
            reblog_button.connect("clicked", lambda *_args: submit("comment"))

            if demo:
                for button in (now_button, schedule_action, reblog_button):
                    button.set_sensitive(False)
                schedule_menu.set_sensitive(False)

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
            # On a hosted page the header already has a Back button; don't show
            # the sidebar toggle in the same slot.
            self.sidebar_toggle.set_visible(
                collapsed and showing_content and self.page_widget is None
            )
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
        if self.page_widget is not None:
            try:
                self._discard_page(self.page_widget)
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
