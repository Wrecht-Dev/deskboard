"""Regression tests for the real DM payload shape (no network, no widgets).

The live route answers ``{"messages": {"data": [...]}}`` and identifies senders
by the participant's ``key`` (``t:<uuid-ish>``), with ``ts`` as a millisecond
digit string.  These tests pin the pure parsing helpers so the UI cannot quietly
fall back to "everything is mine" ordering again.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

try:  # the normal test suite runs without GTK typelibs on headless builders
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except (ImportError, ValueError):  # pragma: no cover - depends on the host
    pytest.skip("GTK typelibs are not available", allow_module_level=True)

from deskboard.ui import messaging  # noqa: E402

CTX = SimpleNamespace(user_name="demo", user_blog="demo")

PARTICIPANTS = [
    {
        "name": "demo",
        "title": "Demo",
        "uuid": "uuid-owner-0001",
        "key": "t:owner-key-aaaa",
        "primary": True,
    },
    {
        "name": "sample_writer",
        "title": "Sample Writer",
        "uuid": "uuid-other-0002",
        "key": "t:other-key-bbbb",
        "primary": False,
    },
]

MESSAGES = [
    {
        "type": "TEXT",
        "ts": "1794427014000",
        "participant": "t:owner-key-aaaa",
        "message": "Synthetic wire text 4",
        "content": {"text": "Synthetic own newest", "formatting": []},
    },
    {
        "type": "TEXT",
        "ts": "1794427011890",
        "participant": "t:other-key-bbbb",
        "message": "Synthetic wire text 1",
        "content": {"text": "Synthetic inbound oldest", "formatting": []},
    },
    {
        "type": "TEXT",
        "ts": "1794427013000",
        "participant": "t:other-key-bbbb",
        "message": "Synthetic wire text 2",
        "content": {"text": "Synthetic inbound middle", "formatting": []},
    },
    {
        "type": "TEXT",
        "ts": "1794427012000",
        "participant": "t:owner-key-aaaa",
        "message": "Synthetic wire text 3",
        "content": {"text": "Synthetic own oldest", "formatting": []},
    },
]

CONVERSATION = {
    "id": "real-shape-1",
    "participants": PARTICIPANTS,
    "messages": {"data": MESSAGES},
    "unreadMessagesCount": 2,
    "lastModifiedTs": 1794427099999,
    "canSend": True,
    "pending": False,
    "isPossibleSpam": False,
}


def test_nested_messages_data_is_used_for_the_snippet():
    last = messaging._conversation_last_message(CONVERSATION)
    assert last is not None
    assert last["ts"] == "1794427014000"
    assert messaging._conversation_snippet(CONVERSATION, CTX) == "Synthetic own newest"


def test_participant_key_aligns_own_and_incoming_messages():
    mine = MESSAGES[0]
    theirs = MESSAGES[1]
    assert messaging._message_is_mine(CTX, CONVERSATION, mine) is True
    assert messaging._message_is_mine(CTX, CONVERSATION, theirs) is False
    assert messaging._message_display_sender(CONVERSATION, theirs, CTX) == "Sample Writer"


def test_unknown_token_falls_back_to_the_primary_participant():
    ambiguous = dict(MESSAGES[0], participant="t:not-in-any-participant")
    assert messaging._message_is_mine(CTX, CONVERSATION, ambiguous) is True


def test_millisecond_ts_drives_display_time_and_ordering():
    assert messaging._message_timestamp(MESSAGES[0]) == 1_794_427_014
    ordered = messaging._chronological(MESSAGES)
    assert [message["ts"] for message in ordered] == [
        "1794427011890",
        "1794427012000",
        "1794427013000",
        "1794427014000",
    ]


def test_pagination_cursor_uses_the_oldest_real_ts():
    assert messaging._message_oldest_ms(MESSAGES) == 1_794_427_011_890
    assert messaging._message_oldest_cursor(MESSAGES) == "1794427011890"


def test_text_comes_from_the_content_object():
    assert messaging._message_text(MESSAGES[0]) == "Synthetic own newest"


def test_owner_filter_and_recipient_use_participant_identity():
    people = messaging._conversation_people(CONVERSATION, CTX)
    assert len(people) == 1
    assert messaging._person_display(people[0]) == "Sample Writer"
    assert messaging._conversation_recipient(CONVERSATION, CTX) == "sample_writer"


def test_message_key_is_unique_at_millisecond_precision():
    twins = [
        dict(MESSAGES[0], ts="1794427014000", content={"text": "same"}),
        dict(MESSAGES[0], ts="1794427014001", content={"text": "same"}),
    ]
    assert messaging._message_key(twins[0]) != messaging._message_key(twins[1])


def test_participant_normalisation_accepts_handle_url_and_object():
    from types import SimpleNamespace

    assert messaging._normalize_participant_name("@Sample_Writer") == "Sample_Writer"
    assert messaging._normalize_participant_name(
        "https://sample_writer.tumblr.com/"
    ) == "sample_writer"
    assert messaging._normalize_participant_name(
        SimpleNamespace(name="sample_writer")
    ) == "sample_writer"
    assert messaging._normalize_participant_name({"name": "sample_writer"}) == "sample_writer"


def test_open_dm_matching_uses_participant_identity():
    assert messaging._conversation_matches_participant(
        CONVERSATION, "sample_writer") is True
    assert messaging._conversation_matches_participant(
        CONVERSATION, "@sample_writer") is True
    assert messaging._conversation_matches_participant(
        CONVERSATION, "t:other-key-bbbb") is True
    # The account owner is listed in every conversation but is not a DM target.
    assert messaging._conversation_matches_participant(CONVERSATION, "demo") is False
    assert messaging._conversation_matches_participant(
        CONVERSATION, "someone_else") is False
