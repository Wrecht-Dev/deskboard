"""Cookie import and session-store tests.

All stores live under pytest's ``tmp_path`` (and ``HOME`` itself is redirected by
``conftest.py``), so no real browser profile or ``~/.local/share`` file is read.
"""
from __future__ import annotations

import json
import stat

from deskboard.api.auth import ImportResult, SessionManager, find_browser_candidates, parse_cookie_text


# --------------------------------------------------------------------------- #
# parse_cookie_text
# --------------------------------------------------------------------------- #

def test_parse_empty_and_garbage():
    assert parse_cookie_text("") == {}
    assert parse_cookie_text("   \n") == {}
    assert parse_cookie_text("this is not a cookie") == {}


def test_parse_netscape_cookies_txt():
    text = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            ".tumblr.com\tTRUE\t/\tTRUE\t0\t_sid\tsecret",
            ".tumblr.com\tTRUE\t/\tFALSE\t0\tsid2\tother",
            "example.com\tTRUE\t/\tFALSE\t0\tforeign\tbad",
            "",
        ]
    )
    assert parse_cookie_text(text) == {"_sid": "secret", "sid2": "other"}


def test_parse_cookie_header():
    assert parse_cookie_text("Cookie: a=b; c=d") == {"a": "b", "c": "d"}
    assert parse_cookie_text("cookie: a=b; c=d") == {"a": "b", "c": "d"}
    assert parse_cookie_text("a=b; c=d") == {"a": "b", "c": "d"}


def test_parse_header_ignores_blank_parts_and_keeps_equals_in_values():
    assert parse_cookie_text("Cookie: a=b=c; ; d=e") == {"a": "b=c", "d": "e"}


def test_parse_json_list_export_filters_foreign_domains():
    text = json.dumps(
        [
            {"name": "a", "value": "1", "domain": ".tumblr.com"},
            {"name": "b", "value": "2", "domain": "example.com"},
            {"name": "c", "value": "3", "host": ".tumblr.com"},
        ]
    )
    assert parse_cookie_text(text) == {"a": "1", "c": "3"}


def test_parse_json_wrapped_export():
    text = json.dumps(
        {"cookies": [{"name": "x", "value": "y", "domain": ".tumblr.com"}]}
    )
    assert parse_cookie_text(text) == {"x": "y"}


def test_parse_json_cookie_map():
    assert parse_cookie_text(json.dumps({"cookies": {"a": "b", "c": "d"}})) == {
        "a": "b",
        "c": "d",
    }


def test_parse_json_rejects_non_tumblr_and_unusable_shapes():
    assert parse_cookie_text(json.dumps([{"name": "a", "value": "1", "domain": "evil.test"}])) == {}
    assert parse_cookie_text(json.dumps({"unexpected": "shape"})) == {}
    assert parse_cookie_text(json.dumps([])) == {}


def test_parse_json_does_not_accept_a_bare_name_value_map_as_cookies():
    # A raw map of names/values has no cookie structure to trust.
    assert parse_cookie_text(json.dumps({"a": "b"})) == {}


# --------------------------------------------------------------------------- #
# SessionManager persistence
# --------------------------------------------------------------------------- #

def test_session_manager_roundtrip_and_file_permissions(tmp_path):
    store = tmp_path / "nested" / "cookies.json"
    manager = SessionManager(store=store)
    assert manager.cookies == {}
    assert manager.has_cookies is False
    assert manager.describe() == "Browsing as a guest"

    result = manager.import_text("Cookie: _sid=secret; other=value")

    assert isinstance(result, ImportResult)
    assert bool(result) is True
    assert result.ok is True
    assert result.count == 2
    assert result.source == "pasted cookies"
    assert manager.cookies == {"_sid": "secret", "other": "value"}
    assert manager.has_cookies is True
    assert manager.describe() == "2 cookies from pasted cookies"
    assert "secret" not in manager.describe()

    assert store.is_file()
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    payload = json.loads(store.read_text("utf-8"))
    assert payload["cookies"] == {"_sid": "secret", "other": "value"}
    assert payload["source"] == "pasted cookies"
    assert isinstance(payload["saved"], int)
    assert not store.with_suffix(".tmp").exists()

    reloaded = SessionManager(store=store)
    assert reloaded.cookies == {"_sid": "secret", "other": "value"}
    assert reloaded.source == "pasted cookies"


def test_session_manager_manual_save_and_clear(tmp_path):
    store = tmp_path / "cookies.json"
    manager = SessionManager(store=store)
    manager.cookies = {"a": "b"}
    manager.source = "manual"
    manager.save()
    assert store.is_file()

    reloaded = SessionManager(store=store)
    assert reloaded.cookies == {"a": "b"}
    assert reloaded.source == "manual"

    reloaded.clear()
    assert reloaded.cookies == {}
    assert reloaded.source == ""
    assert not store.exists()


def test_session_manager_tolerates_corrupt_or_missing_store(tmp_path):
    store = tmp_path / "cookies.json"
    manager = SessionManager(store=store)
    assert manager.load() is False
    assert manager.cookies == {}

    store.write_text("not json at all", "utf-8")
    manager = SessionManager(store=store)
    assert manager.cookies == {}


def test_session_manager_import_file(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(".tumblr.com\tTRUE\t/\tTRUE\t0\t_sid\tvalue\n", "utf-8")
    store = tmp_path / "store.json"
    manager = SessionManager(store=store)
    result = manager.import_file(cookie_file)
    assert bool(result) is True
    assert manager.cookies == {"_sid": "value"}


def test_import_result_truthiness():
    assert bool(ImportResult(True, "ok")) is True
    assert bool(ImportResult(False, "nope")) is False


def test_find_browser_candidates_is_empty_under_temporary_home():
    # conftest redirects HOME, so no real browser profile can be discovered here.
    assert find_browser_candidates() == []
