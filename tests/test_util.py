"""Formatting helpers: compact counts and relative timestamps."""
from __future__ import annotations

import time

import pytest

from deskboard.util import absolute_time, humanize_time, short_number


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (1, "1"),
        (999, "999"),
        (1000, "1K"),
        (1500, "1.5K"),
        (10_500, "10.5K"),
        (1_000_000, "1M"),
        (1_500_000, "1.5M"),
        (2_000_000, "2M"),
    ],
)
def test_short_number(value, expected):
    assert short_number(value) == expected


def test_short_number_handles_missing_values():
    assert short_number(None) == "0"
    assert short_number(0) == "0"


def test_humanize_time_edges():
    now = 1_700_000_000
    assert humanize_time(0, now=now) == ""
    assert humanize_time(now - 0, now=now) == "now"
    assert humanize_time(now - 59, now=now) == "now"
    assert humanize_time(now - 60, now=now) == "1m"
    assert humanize_time(now - 119, now=now) == "1m"
    assert humanize_time(now - 3599, now=now) == "59m"
    assert humanize_time(now - 3600, now=now) == "1h"
    assert humanize_time(now - 86_399, now=now) == "23h"
    assert humanize_time(now - 86_400, now=now) == "1d"
    assert humanize_time(now - 6 * 86_400, now=now) == "6d"


def test_humanize_time_old_posts_use_absolute_month_and_day():
    now = 1_700_000_000
    timestamp = now - 8 * 86_400
    expected = time.strftime("%b %d", time.localtime(timestamp))
    assert humanize_time(timestamp, now=now) == expected


def test_humanize_time_clamps_future_timestamps():
    now = 1_700_000_000
    assert humanize_time(now + 500, now=now) == "now"


def test_absolute_time():
    assert absolute_time(0) == ""
    timestamp = 1_700_000_000
    assert absolute_time(timestamp) == time.strftime(
        "%B %d, %Y at %H:%M", time.localtime(timestamp)
    )
