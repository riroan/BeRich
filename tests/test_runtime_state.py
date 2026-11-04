"""State that must survive a restart round-trips through bot_state."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.bot.runtime_state import RUNTIME_STATE_KEY, RuntimeStateMixin
from src.core import decisions
from src.core.types import Market
from src.strategy.builtin.rsi_mean_reversion import RSIMeanReversionStrategy
from src.utils.notifier import DiscordNotifier


class _Store:
    def __init__(self):
        self.data = {}

    async def get_bot_state(self, key):
        return self.data.get(key)

    async def set_bot_state(self, key, value):
        self.data[key] = value


class _Bot(RuntimeStateMixin):
    def __init__(self, storage):
        self.storage = storage
        self.strategy = RSIMeanReversionStrategy(
            symbols=["AAPL", "NVDA"], market=Market.NASDAQ, params={},
        )
        self.strategy_engine = SimpleNamespace(get_strategies=lambda: [self.strategy])
        self.notifier = DiscordNotifier(webhook_url=None)
        self.order_manager = SimpleNamespace(_buy_backoff={})
        self._running = True


@pytest.fixture(autouse=True)
def _clean_feed():
    decisions.clear()
    yield
    decisions.clear()


@pytest.mark.asyncio
async def test_state_survives_a_restart():
    store = _Store()
    now = datetime.now().replace(microsecond=0)
    before = _Bot(store)
    before.strategy._last_stop_exit_time["AAPL"] = now - timedelta(days=1)
    before.strategy._last_sl_time["NVDA"] = now - timedelta(hours=3)
    before.strategy._last_exit_time["AAPL"] = now - timedelta(days=1)
    before.notifier._throttle[("order_failed", "AAPL", "buy", "code:X")] = {
        "sent_at": now, "suppressed": 4, "last_seen": now,
    }
    before.order_manager._buy_backoff["AAPL"] = now + timedelta(minutes=20)
    decisions.record("AAPL", decisions.BLOCKED, "buy", "손절 후 재진입 쿨다운 3일")
    await before.save_runtime_state()

    decisions.clear()
    after = _Bot(store)        # a new process: everything empty
    await after.load_runtime_state()

    assert after.strategy._last_stop_exit_time["AAPL"] == now - timedelta(days=1)
    assert after.strategy._last_sl_time["NVDA"] == now - timedelta(hours=3)
    assert after.notifier._throttle[("order_failed", "AAPL", "buy", "code:X")]["suppressed"] == 4
    assert after.order_manager._buy_backoff["AAPL"] == now + timedelta(minutes=20)
    assert decisions.recent()[0]["detail"] == "손절 후 재진입 쿨다운 3일"


@pytest.mark.asyncio
async def test_expired_backoff_and_removed_symbols_are_not_restored():
    store = _Store()
    store.data[RUNTIME_STATE_KEY] = json.dumps({
        "strategies": {"RSI Mean Reversion": {
            "_last_exit_time": {"GONE": "2026-11-01T10:00:00"},
        }},
        "buy_backoff": {"AAPL": "2000-01-01T00:00:00"},
    })
    bot = _Bot(store)
    bot.strategy_engine = SimpleNamespace(get_strategies=lambda: [bot.strategy])
    await bot.load_runtime_state()

    assert "GONE" not in bot.strategy._last_exit_time
    assert bot.order_manager._buy_backoff == {}


@pytest.mark.asyncio
async def test_unchanged_state_is_not_rewritten():
    store = _Store()
    bot = _Bot(store)
    await bot.save_runtime_state()
    store.data[RUNTIME_STATE_KEY] = "sentinel"
    await bot.save_runtime_state()
    assert store.data[RUNTIME_STATE_KEY] == "sentinel"


def test_hot_reload_carries_timers_to_the_new_instance():
    old = RSIMeanReversionStrategy(symbols=["AAPL"], market=Market.NASDAQ, params={})
    new = RSIMeanReversionStrategy(symbols=["AAPL"], market=Market.NASDAQ, params={"cooldown_days": 2})
    old._last_stop_exit_time["AAPL"] = datetime(2026, 11, 3, 10)

    RuntimeStateMixin.carry_strategy_timers([old], [new])

    assert new._last_stop_exit_time["AAPL"] == datetime(2026, 11, 3, 10)
