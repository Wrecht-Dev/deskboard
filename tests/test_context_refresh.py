"""Session-refresh hook: browser re-import on a Tumblr 401 must be best effort."""
from __future__ import annotations

import pytest

from deskboard.api.auth import ImportResult, SessionManager
from deskboard.context import AppContext


def _context(tmp_path, source: str = "") -> AppContext:
    """An AppContext with just the session/pubsub state this method touches.

    Constructing a real AppContext would create an ImageCache and read the
    developer's preferences; both are irrelevant to this unit.
    """
    context = object.__new__(AppContext)
    context.session = SessionManager(store=tmp_path / "cookies.json")
    context.session.source = source
    context._listeners = {}
    return context


def _firefox_result(cookies: dict[str, str]) -> ImportResult:
    return ImportResult(
        True,
        f"Imported {len(cookies)} cookies from Firefox (test).",
        len(cookies),
        "Firefox (test)",
    )


def test_refresh_from_firefox_returns_cookies_and_publishes(tmp_path, monkeypatch):
    context = _context(tmp_path, source="Firefox (test)")
    fresh = {"sid": "fresh", "other": "value"}

    def fake_import_firefox(session):
        assert session is context.session
        session.cookies = dict(fresh)
        return _firefox_result(fresh)

    monkeypatch.setattr(SessionManager, "import_firefox", fake_import_firefox)
    published: list[dict[str, str]] = []
    context.subscribe("session-refreshed", lambda cookies: published.append(cookies))

    result = context._refresh_session_from_browser()

    assert result == fresh
    assert context.session.cookies == fresh
    assert published == [fresh]


def test_refresh_without_a_session_source_returns_none_and_does_not_import(
    tmp_path, monkeypatch
):
    context = _context(tmp_path, source="")
    imported: list[bool] = []

    def fake_import_firefox(session):  # noqa: ARG001
        imported.append(True)
        return ImportResult(True, "should not happen", 1, "Firefox (test)")

    monkeypatch.setattr(SessionManager, "import_firefox", fake_import_firefox)
    assert context._refresh_session_from_browser() is None
    assert imported == []


def test_refresh_never_raises_when_the_browser_import_explodes(tmp_path, monkeypatch):
    context = _context(tmp_path, source="Firefox (test)")

    def exploding_import_firefox(session):  # noqa: ARG001
        raise RuntimeError("database is locked")

    monkeypatch.setattr(SessionManager, "import_firefox", exploding_import_firefox)

    assert context._refresh_session_from_browser() is None


def test_refresh_returns_none_when_the_import_finds_nothing(tmp_path, monkeypatch):
    context = _context(tmp_path, source="Firefox (test)")

    monkeypatch.setattr(
        SessionManager,
        "import_firefox",
        lambda session: ImportResult(False, "No Tumblr cookies found.", 0, ""),
    )

    assert context._refresh_session_from_browser() is None
