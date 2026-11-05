"""Recent trading decisions, for the dashboard's activity feed.

The reasons a signal did or did not become an order (paused, duplicate,
in-flight, risk limit, re-entry cooldown...) used to live only in the log.
The order manager and strategies record them here; the web reads them.

Conditions that hold for many ticks (a re-entry cooldown, a pause) would
otherwise flood the feed, so the same (symbol, outcome) is recorded at most
once per ``REPEAT_AFTER``. Account-wide conditions such as a cash shortfall
hit every symbol's buys, so they go in once through ``record_once`` and stay
quiet until ``clear_once`` says the condition is over.
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

# record_once flags
FUNDS = "funds"        # not enough cash to buy; cleared by the next buy order

_entries: deque[dict] = deque(maxlen=MAX_ENTRIES)
_last_seen: dict[tuple[str, str, str], datetime] = {}
_once: set[str] = set()


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


def record_once(
    flag: str,
    symbol: str,
    outcome: str,
    action: str,
    detail: str,
    now: datetime | None = None,
) -> None:
    """Record once per ``flag`` until ``clear_once(flag)``."""
    if flag in _once:
        return
    _once.add(flag)
    record(symbol, outcome, action, detail, now)


def clear_once(flag: str) -> None:
    _once.discard(flag)


def recent(limit: int = 10) -> list[dict]:
    """Newest first."""
    return list(_entries)[:limit]


def export() -> list[dict]:
    """Entries for persisting (newest first); see restore()."""
    return list(_entries)


def restore(entries: list[dict]) -> None:
    """Put persisted entries back, under any recorded since startup."""
    known = {(e["time"], e["symbol"], e["detail"]) for e in _entries}
    for entry in entries[:MAX_ENTRIES]:
        if not isinstance(entry, dict) or "time" not in entry:
            continue
        if (entry["time"], entry.get("symbol"), entry.get("detail")) in known:
            continue
        if len(_entries) >= MAX_ENTRIES:
            break
        _entries.append(dict(entry))


def clear() -> None:
    _entries.clear()
    _last_seen.clear()
    _once.clear()
