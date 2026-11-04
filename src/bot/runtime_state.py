"""Runtime state that must survive a restart, kept in the bot_state table.

Positions, ladder stages and last buy/sell dates already persist through
current_positions. What did not, and silently reset on every restart (a
Kubernetes reschedule included):

- strategy timers: when a symbol last went flat, last stopped out (gates
  re-entry for stop_loss_reentry_days) and last filled a stop rung (re-arms
  the loss ladder after cooldown_days);
- the notifier's repeat-suppression for failure alerts;
- the order manager's buy backoff after an insufficient-funds rejection;
- the dashboard's activity feed.

They are saved as one JSON document under RUNTIME_STATE_KEY: restored before
trading starts, saved every SAVE_INTERVAL seconds when it changed, and once
on shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from src.core import decisions

logger = logging.getLogger(__name__)

RUNTIME_STATE_KEY = "runtime_state"
SAVE_INTERVAL = 30
# Strategy attributes holding {symbol: datetime}
STRATEGY_TIMERS = ("_last_exit_time", "_last_stop_exit_time", "_last_sl_time")
# Suppressed alerts older than this are not worth carrying over
THROTTLE_MAX_AGE = timedelta(days=1)


def _dict_attr(obj: Any, name: str) -> dict:
    value = getattr(obj, name, None)
    return value if isinstance(value, dict) else {}


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class RuntimeStateMixin:
    """Collect / apply / persist the runtime state above."""

    # Provided by TradingBot
    storage: Any
    strategy_engine: Any
    notifier: Any
    order_manager: Any
    _running: bool

    _runtime_state_saved: str | None = None
    _runtime_state_task: asyncio.Task | None = None

    # ---------- collect / apply ----------

    def collect_runtime_state(self) -> dict:
        now = datetime.now()
        strategies: dict[str, dict] = {}
        for strategy in (
            self.strategy_engine.get_strategies() if self.strategy_engine else []
        ):
            timers = {
                attr: {sym: _iso(t) for sym, t in getattr(strategy, attr).items()}
                for attr in STRATEGY_TIMERS
                if getattr(strategy, attr, None)
            }
            if timers:
                strategies[strategy.name] = timers

        throttle = [
            {
                "key": list(key),
                "sent_at": _iso(st["sent_at"]),
                "suppressed": st["suppressed"],
                "last_seen": _iso(st["last_seen"]),
            }
            for key, st in _dict_attr(self.notifier, "_throttle").items()
            if now - st["last_seen"] < THROTTLE_MAX_AGE
        ]
        backoff = {
            sym: _iso(until)
            for sym, until in _dict_attr(self.order_manager, "_buy_backoff").items()
            if until > now
        }
        return {
            "strategies": strategies,
            "alert_throttle": throttle,
            "buy_backoff": backoff,
            "decisions": decisions.export(),
        }

    def apply_runtime_state(self, state: dict) -> None:
        self.apply_strategy_timers(state.get("strategies") or {})

        throttle = getattr(self.notifier, "_throttle", None)
        if throttle is not None:
            for entry in state.get("alert_throttle") or []:
                sent, seen = _parse(entry.get("sent_at")), _parse(entry.get("last_seen"))
                if sent and seen:
                    throttle[tuple(entry["key"])] = {
                        "sent_at": sent,
                        "suppressed": int(entry.get("suppressed", 0)),
                        "last_seen": seen,
                    }

        backoff = getattr(self.order_manager, "_buy_backoff", None)
        if backoff is not None:
            for sym, until in (state.get("buy_backoff") or {}).items():
                if (when := _parse(until)) and when > datetime.now():
                    backoff[sym] = when

        decisions.restore(state.get("decisions") or [])

    def apply_strategy_timers(self, timers_by_name: dict) -> None:
        for strategy in (
            self.strategy_engine.get_strategies() if self.strategy_engine else []
        ):
            timers = timers_by_name.get(strategy.name) or {}
            for attr in STRATEGY_TIMERS:
                target = getattr(strategy, attr, None)
                if target is None:
                    continue
                for sym, value in (timers.get(attr) or {}).items():
                    if sym in strategy.symbols and (when := _parse(value)):
                        target.setdefault(sym, when)

    @staticmethod
    def carry_strategy_timers(old: list, new: list) -> None:
        """Hot reload builds fresh instances; keep the old ones' timers."""
        by_name = {s.name: s for s in old}
        for strategy in new:
            previous = by_name.get(strategy.name)
            if previous is None or previous is strategy:
                continue
            for attr in STRATEGY_TIMERS:
                source = getattr(previous, attr, None)
                target = getattr(strategy, attr, None)
                if source and target is not None:
                    for sym, when in source.items():
                        if sym in strategy.symbols:
                            target.setdefault(sym, when)

    # ---------- persist ----------

    async def load_runtime_state(self) -> None:
        if not self.storage:
            return
        try:
            raw = await self.storage.get_bot_state(RUNTIME_STATE_KEY)
            if raw:
                self.apply_runtime_state(json.loads(raw))
                self._runtime_state_saved = raw
                logger.info("Runtime state restored")
        except Exception as e:
            logger.warning(f"Failed to restore runtime state: {e}")

    async def save_runtime_state(self) -> None:
        if not self.storage:
            return
        try:
            raw = json.dumps(
                self.collect_runtime_state(), ensure_ascii=False, sort_keys=True,
            )
            if raw == self._runtime_state_saved:
                return
            await self.storage.set_bot_state(RUNTIME_STATE_KEY, raw)
            self._runtime_state_saved = raw
        except Exception as e:
            logger.warning(f"Failed to save runtime state: {e}")

    async def _runtime_state_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(SAVE_INTERVAL)
                await self.save_runtime_state()
            except asyncio.CancelledError:
                break
