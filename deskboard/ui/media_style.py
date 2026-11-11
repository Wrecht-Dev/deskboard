"""Loader for the media module's private stylesheet.  Owner: media."""
from __future__ import annotations

from pathlib import Path

_INSTALLED = False


def install() -> None:
    """Attach ``media.css`` to the default display once, at application priority."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gtk

        path = Path(__file__).with_name("media.css")
        if not path.is_file():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(path))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
    except Exception:  # noqa: BLE001 - a missing stylesheet must never break a card
        return
