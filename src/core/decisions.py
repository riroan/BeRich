"""Recent trading decisions, for the dashboard's activity feed.

The reasons a signal did or did not become an order (paused, duplicate,
in-flight, risk limit, re-entry cooldown...) used to live only in the log.
The order manager and strategies record them here; the web reads them.

Conditions that hold for many ticks (a re-entry cooldown, a pause) would
otherwise flood the feed, so the same (symbol, outcome) is recorded at most
once per ``REPEAT_AFTER``.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

MAX_ENTRIES = 50
REPEAT_AFTER = timedelta(minutes=30)

# Outcome keys, shown as tags in the feed
ORDER = "order"        # an order was submitted
BLOCKED = "blocked"    # a gate held the signal back on purpose
REJECTED = "rejected"  # a risk limit or error stopped it

_entries: deque[dict] = deque(maxlen=MAX_ENTRIES)
_last_seen: dict[tuple[str, str, str], datetime] = {}


def record(
    symbol: str,
    outcome: str,
    action: str,
    detail: str,
    now: datetime | None = None,
) -> None:
    """Add a decision. ``action`` is 'buy' or 'sell'."""
    now = now or datetime.now()
    key = (symbol, outcome, detail)
    last = _last_seen.get(key)
    if outcome != ORDER and last is not None and now - last < REPEAT_AFTER:
        return
    _last_seen[key] = now
    _entries.appendleft({
        "time": now.isoformat(timespec="seconds"),
        "symbol": symbol,
        "outcome": outcome,
        "action": action,
        "detail": detail,
    })


def recent(limit: int = 10) -> list[dict]:
    """Newest first."""
    return list(_entries)[:limit]


def clear() -> None:
    _entries.clear()
    _last_seen.clear()
