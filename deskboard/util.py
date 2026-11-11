"""Small formatting helpers shared by widgets."""
from __future__ import annotations

import time


def short_number(value: int) -> str:
    value = int(value or 0)
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        text = f"{value / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{text}K"
    text = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
    return f"{text}M"


def humanize_time(timestamp: int, now: float | None = None) -> str:
    if not timestamp:
        return ""
    now = now or time.time()
    delta = max(0, int(now - timestamp))
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    if delta < 86400 * 7:
        return f"{delta // 86400}d"
    return time.strftime("%b %d", time.localtime(timestamp))


def absolute_time(timestamp: int) -> str:
    if not timestamp:
        return ""
    return time.strftime("%B %d, %Y at %H:%M", time.localtime(timestamp))
