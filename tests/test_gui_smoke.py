"""Optional GUI smoke test.

The normal suite deliberately never imports ``deskboard.ui``: those modules need
GTK 4, libadwaita, typelibs and a display, and would make ``pytest -q`` fail on a
headless build machine.  This test is opt-in and runs the import + CSS parse in a
throw-away X server:

    . scripts/env.sh
    DESKBOARD_GUI_TESTS=1 xvfb-run -a python3 -m pytest -q tests/test_gui_smoke.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_SMOKE_SCRIPT = r"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

Gtk.init()
Adw.init()

from deskboard.app import load_css
from deskboard.demo import DemoClient

load_css()

# Import every UI module (not just the ones the window loads eagerly) and make
# sure the offline client can be built.  No application main loop is started.
import deskboard.ui.window          # noqa: F401
import deskboard.ui.sidebar         # noqa: F401
import deskboard.ui.feed            # noqa: F401
import deskboard.ui.postcard        # noqa: F401
import deskboard.ui.blocks          # noqa: F401
import deskboard.ui.html            # noqa: F401
import deskboard.ui.mediawidgets    # noqa: F401
import deskboard.ui.viewer          # noqa: F401
import deskboard.ui.onboarding      # noqa: F401
import deskboard.ui.settings        # noqa: F401

client = DemoClient()
assert client.get_dashboard().posts
print("deskboard-gui-smoke-ok")
"""


@pytest.mark.skipif(
    os.environ.get("DESKBOARD_GUI_TESTS") != "1",
    reason="set DESKBOARD_GUI_TESTS=1 and run under xvfb-run to test the GUI",
)
def test_ui_modules_import_and_css_parses_under_xvfb(tmp_path):
    runner = shutil.which("xvfb-run")
    if not runner:
        pytest.skip("xvfb-run is not installed")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    # If the private typelib bundle is present but env.sh was not sourced, use it.
    gir = Path.home() / ".local/share/deskboard/gir"
    if gir.is_dir():
        env["GI_TYPELIB_PATH"] = str(gir) + (
            os.pathsep + env["GI_TYPELIB_PATH"] if env.get("GI_TYPELIB_PATH") else ""
        )
    # Keep the smoke test away from the real profile.
    env["XDG_DATA_HOME"] = str(tmp_path / "data")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")

    # Skip cleanly when GTK 4 cannot be imported on this machine (no typelibs,
    # no display server available for the probe, broken PyGObject, ...).
    probe = subprocess.run(
        [sys.executable, "-c",
         'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")'],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        pytest.skip("GTK 4 is not importable here: " + (detail[-1] if detail else "unknown error"))

    result = subprocess.run(
        [
            runner,
            "-a",
            "-s",
            "-screen 0 1280x800x24",
            sys.executable,
            "-c",
            _SMOKE_SCRIPT,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, output
    assert "deskboard-gui-smoke-ok" in result.stdout
