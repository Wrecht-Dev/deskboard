"""Shared pytest setup for Deskboard.

The tests exercise the *headless* half of the app (models, API client, auth
helpers, utilities and the demo client).  Nothing here imports GTK, so the
suite runs on a machine without a display and without a working typelib bundle.

Two hermeticity guarantees:

* the repository root is put on ``sys.path`` so ``python3 -m pytest`` works even
  when ``scripts/env.sh`` was not sourced;
* ``HOME`` and the XDG directories point at a throw-away directory, so tests can
  never read or write the developer's real cookies, preferences or cache.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "data" / "fixtures"

# Make ``import deskboard`` reliable regardless of the current directory.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep the suite away from the real user profile.  This happens before any test
# module imports deskboard.config, which snapshots these values at import time.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="deskboard-test-home-"))
for _var, _sub in {
    "HOME": "",
    "XDG_DATA_HOME": "data",
    "XDG_CONFIG_HOME": "config",
    "XDG_CACHE_HOME": "cache",
}.items():
    _path = _TEST_HOME / _sub if _sub else _TEST_HOME
    _path.mkdir(parents=True, exist_ok=True)
    os.environ[_var] = str(_path)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest hook
    """Remove the temporary home once the whole suite is done."""
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def fixture_payloads() -> dict[str, dict]:
    """Every ``data/fixtures/*.json`` payload, keyed by file name."""
    payloads: dict[str, dict] = {}
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        try:
            payloads[path.name] = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):  # pragma: no cover - defensive
            continue
    return payloads


@pytest.fixture(scope="session")
def fixture_posts(fixture_payloads: dict[str, dict]) -> list:
    """All posts parsed from the bundled captures."""
    from deskboard.models import parse_posts

    posts = []
    for payload in fixture_payloads.values():
        response = payload.get("response", payload)
        posts.extend(parse_posts(response))
    return posts
