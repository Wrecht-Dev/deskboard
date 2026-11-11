"""Application bootstrap.

Owner: shell.
"""
from __future__ import annotations

import sys
from pathlib import Path


def load_css() -> None:
    """Load ``ui/style.css`` once; a malformed rule must not kill the app."""
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    css_path = Path(__file__).with_name("ui") / "style.css"
    if not css_path.exists():
        return
    try:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
    except GLib.Error as exc:  # pragma: no cover - only on a broken stylesheet
        sys.stderr.write(f"Deskboard: could not load stylesheet: {exc.message}\n")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"Deskboard: could not load stylesheet: {exc}\n")
    # The media module owns its own prefixed sheet; install() is idempotent.
    try:
        from .ui.media_style import install as install_media_css

        install_media_css()
    except Exception:  # noqa: BLE001 - missing media module must not break startup
        pass


def run(demo: bool = False, cookies_path: str | None = None, feed: str | None = None) -> int:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio

    from . import config
    from .context import AppContext
    from .ui.window import MainWindow

    Adw.init()
    application = Adw.Application(
        application_id=config.APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS
    )
    ctx = AppContext(demo=demo, cookies_path=cookies_path)

    if application.lookup_action("quit") is None:
        quit_action = Gio.SimpleAction.new("quit", None)

        def _on_quit(*_args) -> None:
            # Close the window first so ``close-request`` can persist geometry.
            window = getattr(ctx, "window", None)
            if window is not None:
                try:
                    window.close()
                    return
                except Exception:  # noqa: BLE001
                    pass
            application.quit()

        quit_action.connect("activate", _on_quit)
        application.add_action(quit_action)
    try:
        application.set_accels_for_action("app.quit", ["<Control>q"])
    except Exception:  # noqa: BLE001
        pass

    def on_activate(app) -> None:
        try:
            load_css()
            from .ui.settings import apply_theme

            apply_theme(ctx)
            window = MainWindow(app, ctx)
            window.present()
            window.apply_startup_state()
            window.open_initial(feed)
            if not demo and ctx.is_signed_in:
                ctx.load_user(window.on_user_loaded)
        except Exception as exc:  # noqa: BLE001 - report instead of a traceback storm
            sys.stderr.write(f"Deskboard could not start: {exc}\n")
            app.quit()

    def on_shutdown(_app) -> None:
        # Last-chance save for quits that bypass close-request (e.g. session end).
        try:
            window = getattr(ctx, "window", None)
            if window is not None:
                window.save_state()
        except Exception:  # noqa: BLE001
            pass
        ctx.shutdown()

    application.connect("activate", on_activate)
    application.connect("shutdown", on_shutdown)
    return application.run([])
