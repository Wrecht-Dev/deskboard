"""Sign-in page: borrow the Tumblr session from the browser.

Owner: shell.

Tumblr has no OAuth flow for third-party desktop clients, so the only way to
see a dashboard is to reuse the cookies of a browser that is already signed in.
This page offers the three practical ways to get them (Firefox, Chromium family,
or a pasted cookie header) plus a guest escape hatch.

Cookie *values* are never displayed, logged or put into a message; only counts
and the source name come back from the importer.
"""
from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk


class OnboardingPage(Gtk.Box):
    __gtype_name__ = "DeskboardOnboardingPage"

    def __init__(self, ctx, on_ready=None, on_browse=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.on_ready = on_ready
        self.on_browse = on_browse
        self._busy = False
        self._opened = False
        self._buttons: list[Gtk.Widget] = []

        self._build()

    # -- construction ------------------------------------------------------ #
    def _build(self) -> None:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrapper.set_valign(Gtk.Align.CENTER)
        wrapper.set_vexpand(True)
        wrapper.set_margin_top(28)
        wrapper.set_margin_bottom(28)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_start(24)
        content.set_margin_end(24)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(560)
        clamp.set_tightening_threshold(420)
        clamp.set_child(content)
        wrapper.append(clamp)
        scroller.set_child(wrapper)
        self.append(scroller)

        icon = Gtk.Image.new_from_icon_name("system-users-symbolic")
        icon.set_pixel_size(64)
        icon.set_halign(Gtk.Align.CENTER)
        icon.add_css_class("onboarding-icon")
        content.append(icon)

        title = Gtk.Label(label="Welcome to Deskboard")
        title.add_css_class("title-1")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        content.append(title)

        subtitle = Gtk.Label(label="Your Tumblr dashboard, native and fast.")
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        content.append(subtitle)

        explanation = Gtk.Label(
            label=(
                "Tumblr does not offer a desktop sign-in, so Deskboard borrows the "
                "session from the browser you are already signed into. Nothing is "
                "uploaded and cookies never leave this computer."
            )
        )
        explanation.set_wrap(True)
        explanation.set_justify(Gtk.Justification.LEFT)
        explanation.set_xalign(0)
        explanation.set_margin_top(4)
        explanation.add_css_class("onboarding-explanation")
        content.append(explanation)

        import_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        import_box.set_margin_top(6)
        import_heading = Gtk.Label(label="Import the session from", xalign=0)
        import_heading.add_css_class("caption-heading")
        import_box.append(import_heading)

        firefox = self._make_button(
            "web-browser-symbolic", "Import from Firefox", "suggested-action"
        )
        firefox.connect("clicked", lambda *_args: self._start_import("firefox"))
        import_box.append(firefox)

        chromium = self._make_button(
            "web-browser-symbolic", "Import from Chromium", None
        )
        chromium.connect("clicked", lambda *_args: self._start_import("chromium"))
        import_box.append(chromium)
        content.append(import_box)

        content.append(self._build_paste_expander())

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status_box.set_margin_top(2)
        self.spinner = Adw.Spinner()
        self.spinner.set_size_request(18, 18)
        self.spinner.set_valign(Gtk.Align.START)
        self.spinner.set_visible(False)
        status_box.append(self.spinner)
        self.status_label = Gtk.Label(label="")
        self.status_label.set_wrap(True)
        self.status_label.set_xalign(0)
        self.status_label.set_hexpand(True)
        self.status_label.set_visible(False)
        self.status_label.add_css_class("onboarding-status")
        status_box.append(self.status_label)
        content.append(status_box)

        browse = Gtk.Button()
        browse.set_child(
            Adw.ButtonContent(icon_name="web-browser-symbolic", label="Browse without signing in")
        )
        browse.add_css_class("flat")
        browse.set_halign(Gtk.Align.CENTER)
        browse.connect("clicked", self._on_browse_clicked)
        self._buttons.append(browse)
        content.append(browse)

        privacy = Gtk.Label(
            label=(
                "Cookies stay on this device in a private file. "
                "Signing out removes them."
            )
        )
        privacy.set_wrap(True)
        privacy.set_justify(Gtk.Justification.CENTER)
        privacy.add_css_class("caption")
        privacy.add_css_class("dim-label")
        privacy.set_margin_top(6)
        content.append(privacy)

    def _make_button(self, icon_name: str, label: str, css: str | None) -> Gtk.Button:
        button = Gtk.Button()
        button.set_child(Adw.ButtonContent(icon_name=icon_name, label=label))
        button.set_halign(Gtk.Align.FILL)
        if css:
            button.add_css_class(css)
        self._buttons.append(button)
        return button

    def _build_paste_expander(self) -> Gtk.Widget:
        expander = Gtk.Expander()
        expander.set_label("Paste cookies instead")
        expander.set_margin_top(2)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_top(10)
        inner.set_margin_start(2)
        inner.set_margin_end(2)

        hint = Gtk.Label(
            label=(
                "In a browser signed into Tumblr, open the developer tools, copy the "
                "Cookie request header for tumblr.com, and paste it here."
            )
        )
        hint.set_wrap(True)
        hint.set_xalign(0)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        inner.append(hint)

        text_view = Gtk.TextView()
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_top_margin(8)
        text_view.set_bottom_margin(8)
        text_view.set_left_margin(8)
        text_view.set_right_margin(8)
        text_view.set_monospace(True)
        self._paste_buffer = text_view.get_buffer()

        frame = Gtk.Frame()
        frame.set_child(text_view)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(frame)
        scroller.set_min_content_height(110)
        scroller.set_vexpand(True)
        inner.append(scroller)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.set_halign(Gtk.Align.END)
        paste_button = Gtk.Button(label="Import pasted cookies")
        paste_button.add_css_class("pill")
        paste_button.connect("clicked", self._on_paste_import)
        self._buttons.append(paste_button)
        row.append(paste_button)
        inner.append(row)

        expander.set_child(inner)
        return expander

    # -- import flow ------------------------------------------------------- #
    def _start_import(self, kind: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._set_status("Looking for Tumblr cookies…", ok=True)
        session = self.ctx.session

        def work() -> None:
            ok = False
            message = ""
            try:
                if kind == "firefox":
                    result = session.import_firefox()
                else:
                    result = session.import_chromium()
                ok = bool(getattr(result, "ok", False))
                message = str(getattr(result, "message", "") or "")
            except Exception as exc:  # noqa: BLE001 - report, never crash
                ok = False
                message = f"Could not read the browser cookies: {exc}"
            try:
                GLib.idle_add(self._apply_import_result, ok, message, False)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name=f"import-{kind}").start()
        except Exception as exc:  # noqa: BLE001
            self._apply_import_result(False, f"Could not start the import: {exc}", False)

    def _on_paste_import(self, *_args) -> None:
        if self._busy:
            return
        try:
            text = self._paste_buffer.get_text(
                self._paste_buffer.get_start_iter(),
                self._paste_buffer.get_end_iter(),
                True,
            )
        except Exception:  # noqa: BLE001
            text = ""
        if not text.strip():
            self._set_status("Paste the Tumblr cookie header first.", ok=False)
            return

        self._set_busy(True)
        self._set_status("Reading the pasted cookies…", ok=True)
        session = self.ctx.session

        def work() -> None:
            ok = False
            message = ""
            try:
                result = session.import_text(text)
                ok = bool(getattr(result, "ok", False))
                message = str(getattr(result, "message", "") or "")
            except Exception as exc:  # noqa: BLE001
                ok = False
                message = f"Could not read the pasted cookies: {exc}"
            try:
                GLib.idle_add(self._apply_import_result, ok, message, True)
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="import-paste").start()
        except Exception as exc:  # noqa: BLE001
            self._apply_import_result(False, f"Could not start the import: {exc}", True)

    def _apply_import_result(self, ok: bool, message: str, from_paste: bool) -> bool:
        try:
            self._set_busy(False)
            if not ok:
                self._set_status(message or "No Tumblr cookies were found.", ok=False)
                self.ctx.toast(message or "No Tumblr cookies were found.")
                return False
            if from_paste:
                try:
                    self._paste_buffer.set_text("")
                except Exception:  # noqa: BLE001
                    pass
            self._adopt_and_verify(message)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"The import failed: {exc}", ok=False)
            self.ctx.toast(f"The import failed: {exc}")
        return False

    def _adopt_and_verify(self, message: str) -> None:
        """Hand the imported cookies to the context, then check the account."""
        try:
            cookies = dict(self.ctx.session.cookies)
            source = str(getattr(self.ctx.session, "source", "") or "")
            self.ctx.adopt_session(cookies, source)
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Could not use the imported session: {exc}", ok=False)
            self.ctx.toast(f"Could not use the imported session: {exc}")
            return
        self._set_status(message, ok=True)
        self.ctx.toast(message)
        self._set_busy(True)
        self._set_status(f"{message} Checking the account…", ok=True)
        try:
            self.ctx.load_user(self._on_user_verified)
        except Exception as exc:  # noqa: BLE001 - open the dashboard anyway
            self._on_user_verified(False)

    def _on_user_verified(self, ok: bool) -> None:
        try:
            self._set_busy(False)
            name = str(getattr(self.ctx, "user_name", "") or "")
            if ok and name:
                self._set_status(f"Signed in as {name}.", ok=True)
                self.ctx.toast(f"Signed in as {name}")
            else:
                self._set_status(
                    "The session was imported, but the account could not be verified. "
                    "Check your connection — your dashboard may still load.",
                    ok=False,
                )
                self.ctx.toast("Session imported; account verification failed")
        except Exception:  # noqa: BLE001
            pass
        self._open_dashboard()

    def _open_dashboard(self) -> None:
        # Leave the inline result on screen for a moment so the user actually
        # sees it (a toast alone disappears too quickly to read).
        if self._opened:
            return
        self._opened = True

        def go() -> bool:
            try:
                if self.get_root() is None:
                    return False
                if callable(self.on_ready):
                    self.on_ready()
            except Exception:  # noqa: BLE001
                pass
            return False

        try:
            GLib.timeout_add(550, go)
        except Exception:  # noqa: BLE001
            go()

    # -- small helpers ----------------------------------------------------- #
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        try:
            self.spinner.set_visible(busy)
        except Exception:  # noqa: BLE001
            pass
        for button in self._buttons:
            try:
                button.set_sensitive(not busy)
            except Exception:  # noqa: BLE001
                pass

    def _set_status(self, message: str, ok: bool = True) -> None:
        try:
            self.status_label.set_text(str(message or ""))
            self.status_label.set_visible(bool(message))
            if ok:
                self.status_label.remove_css_class("error")
            else:
                self.status_label.add_css_class("error")
        except Exception:  # noqa: BLE001
            pass

    def _on_browse_clicked(self, *_args) -> None:
        if callable(self.on_browse):
            try:
                self.on_browse()
            except Exception:  # noqa: BLE001
                pass
