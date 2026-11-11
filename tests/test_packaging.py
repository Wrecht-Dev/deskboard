"""Packaging checks: icon, desktop entry, scripts, pyproject and README."""
from __future__ import annotations

import configparser
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "assets" / "icon.svg"
DESKTOP = ROOT / "deskboard.desktop"


def test_svg_icon_is_well_formed_and_self_contained():
    assert SVG.is_file(), f"missing icon: {SVG}"
    tree = ET.parse(SVG)
    root = tree.getroot()
    assert root.tag.endswith("svg")
    assert root.get("viewBox") == "0 0 1024 1024"
    assert root.get("width") in (None, "1024")
    assert root.get("height") in (None, "1024")

    text = SVG.read_text("utf-8")
    assert "#36465d" in text.lower(), "expected Tumblr blue in the icon"
    # No external assets, scripts or raster references.
    assert "<script" not in text.lower()
    for element in root.iter():
        for attribute, value in element.attrib.items():
            if attribute.endswith("href"):
                assert not value.startswith(("http:", "https:", "data:"))
        assert not element.tag.endswith("image")


def test_optional_png_icon_has_png_signature():
    png = ROOT / "assets" / "icon.png"
    if not png.exists():
        pytest.skip("assets/icon.png not rendered on this machine")
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def _desktop_parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(DESKTOP, encoding="utf-8")
    return parser


def test_desktop_entry_required_fields():
    assert DESKTOP.is_file(), f"missing desktop entry: {DESKTOP}"
    parser = _desktop_parser()
    assert "Desktop Entry" in parser
    entry = parser["Desktop Entry"]

    assert entry["Type"] == "Application"
    assert entry["Name"] == "Deskboard"
    assert entry["Comment"] == "A fast, native Tumblr dashboard"
    assert entry["Exec"] == "deskboard"
    assert entry["Icon"] == "dev.wrecht.Deskboard"
    assert entry["Terminal"] == "false"
    assert entry["StartupWMClass"] == "dev.wrecht.Deskboard"

    categories = [item for item in entry["Categories"].split(";") if item]
    assert "Network" in categories
    assert "Feed" in categories
    assert entry["Keywords"].endswith(";")


def test_desktop_entry_validates_with_freedesktop_tool():
    validator = shutil.which("desktop-file-validate")
    if not validator:
        pytest.skip("desktop-file-validate is not installed")
    result = subprocess.run(
        [validator, str(DESKTOP)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")


def test_pyproject_metadata():
    if tomllib is None:
        pytest.skip("tomllib needs Python 3.11+")
    assert (ROOT / "pyproject.toml").is_file()
    data = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    assert data["project"]["name"] == "deskboard"
    assert "requires-python" in data["project"]
    assert data["project"]["scripts"]["deskboard"] == "deskboard.__main__:main"
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"
    assert data["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


@pytest.mark.parametrize(
    "script",
    ["scripts/bootstrap-gir.sh", "scripts/run.sh", "scripts/install.sh"],
)
def test_scripts_are_executable_and_parse(script):
    path = ROOT / script
    assert path.is_file(), f"missing script: {script}"
    assert path.stat().st_mode & 0o111, f"{script} is not executable"
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_readme_covers_the_important_paths():
    readme = (ROOT / "README.md").read_text("utf-8").lower()
    for needle in (
        "bootstrap-gir.sh",
        "install.sh",
        "--doctor",
        "--demo",
        "pytest",
        "unofficial",
        "privacy",
        "www.tumblr.com",
    ):
        assert needle in readme, f"README does not mention {needle!r}"


def test_gitignore_covers_generated_files():
    text = (ROOT / ".gitignore").read_text("utf-8")
    for pattern in ("__pycache__/", "*.pyc", ".pytest_cache/", "data/cache/", ".venv/"):
        assert pattern in text, f".gitignore does not ignore {pattern!r}"
    assert "screenshot" in text
