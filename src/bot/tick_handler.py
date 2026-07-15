"""Tick handler for trading bot"""

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
import logging

from src.bot._utils import extract_symbols
from src.core.events import Event, EventType
from src.core.types import Bar
from src.utils.scheduler import daytime_tag

if TYPE_CHECKING:
    from src.bot.core import TradingBot

logger = logging.getLogger(__name__)


class TickHandlerMixin:
    """Mixin for tick handling methods"""

    async def on_tick(self: "TradingBot") -> None:
        """Called every minute - fetch prices and check strategies"""
        if getattr(self.dashboard, 'debug_freeze', False):
            return
        await self._warmup.log_status()
        # React to US session changes: RSI base slide + stop-loss re-price.
        await self._handle_session_transition()
        await self.update_dashboard_status()
        await self.update_dashboard_positions()
        # Symbol reconciliation runs in _config_sync_loop (market-independent),
        # not here — on_tick is gated behind is_market_open().

        logger.debug(f"Tick: {datetime.now():%H:%M:%S}")

        for strategy in self.strategy_engine.get_strategies():
            for symbol in strategy.symbols:
                await self._process_symbol_tick(strategy, symbol)

        self.dashboard.update_system_status(
            auto_trading=await self._warmup.is_complete(),
            api_connected=True,
            account_tradable=True,
            data_ok=True,
        )

        await self.broadcast_tick_update()

    async def _sync_enabled_symbols(self: "TradingBot") -> None:
        """Sync strategy symbols with DB (strategy_configs)"""
        try:
            from collections import defaultdict

            configs = (
                await self.storage.get_all_strategy_configs()
            )
            symbols_by_strategy = defaultdict(set)
            for cfg in configs:
                if not cfg["enabled"]:
                    continue
                symbols_by_strategy[cfg["name"]].update(
                    extract_symbols(cfg["symbols"])
                )

            for strategy in self.strategy_engine.get_strategies():
                name = strategy.name_with_market
                enabled = symbols_by_strategy.get(name, set())
                current = set(strategy.symbols)

                if enabled != current:
                    added = enabled - current
                    removed = current - enabled
                    strategy.symbols = list(enabled)

                    for symbol in added:
                        try:
                            await asyncio.sleep(0.5)
                            bars = await self.broker.get_historical_bars(
                                symbol=symbol,
                                market=strategy.market,
                                days=strategy.required_history,
                            )
                            if bars and hasattr(strategy, "initialize"):
                                strategy.initialize({symbol: bars})
                            logger.info(
                                f"[{symbol}] Loaded {len(bars)} "
                                f"bars (newly enabled)"
                            )
                        except Exception as e:
                            logger.error(
                                f"[{symbol}] Failed to load "
                                f"history: {e}"
                            )

                    for symbol in removed:
                        self.dashboard.rsi_values.pop(symbol, None)
                        self.dashboard.rsi_prices.pop(symbol, None)

                    logger.info(
                        f"[{name}] Symbols synced: "
                        f"{sorted(enabled)}"
                    )
        except Exception as e:
            logger.debug(f"Symbol sync error: {e}")

    async def _process_symbol_tick(self: "TradingBot", strategy, symbol: str) -> None:
        """Process tick for a single symbol"""
        try:
            # Rate limit: wait between API calls
            await asyncio.sleep(1)

            price = await self.broker.get_current_price(symbol, strategy.market)

            if not price or price <= 0:
                logger.warning(f"[{symbol}] Invalid price: {price}")
                return

            if hasattr(strategy, "update_daily_close"):
                strategy.update_daily_close(symbol, float(price))

            bar = Bar(
                symbol=symbol,
                market=strategy.market,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=0,
                timestamp=datetime.now(),
                timeframe="1d",
            )

            await self.event_bus.publish(
                Event(
                    event_type=EventType.BAR_UPDATE,
                    data=bar,
                    timestamp=datetime.now(),
                    source="Scheduler",
                )
            )

            rsi = None
            rsi_str = ""
            if hasattr(strategy, "get_current_rsi"):
                if (rsi := strategy.get_current_rsi(symbol)) is not None:
                    rsi_str = f" | RSI: {rsi:.1f}"

            # Corporate-action guard: if the RSI window spans a split/merge
            # cliff, alert and DON'T record the split-corrupted RSI (trading
            # is already suppressed in the strategy). Keeps chart/history
            # clean until an adjusted-price re-fetch (restart) heals the base.
            corp_ratio = (
                strategy.corporate_action_ratio(symbol)
                if hasattr(strategy, "corporate_action_ratio") else None
            )
            if corp_ratio is not None:
                await self._alert_corporate_action(symbol, corp_ratio)
                rsi = None
                rsi_str = " | RSI: suppressed (corp-action)"

            await self.storage.save_price_rsi(
                symbol=symbol,
                market=strategy.market,
                price=price,
                rsi=rsi,
            )

            if rsi is not None:
                self.dashboard.update_rsi(
                    symbol, rsi, price=float(price),
                    market=strategy.market.value.upper(),
                )
                self.dashboard.add_rsi_point(symbol, datetime.now(), rsi)
                self.dashboard.add_price_point(
                    symbol=symbol,
                    time=datetime.now(),
                    open_=float(price),
                    high=float(price),
                    low=float(price),
                    close=float(price),
                )

            self.dashboard.last_price_update = datetime.now()
            # 주간거래 세션 tick은 로그에서 바로 구분되도록 마커를 붙인다
            # (정규 외 시간엔 BAQ/BAY/BAA venue로 시세 조회 — 별도 추적용).
            logger.info(
                f"{daytime_tag(self._last_session)}[{symbol}] "
                f"Price: {price:,}{rsi_str}"
            )

        except Exception as e:
            logger.error(f"Error fetching {symbol}: {e}")
            if self.notifier:
                await self.notifier.notify_data_fetch_failed(
                    symbol=symbol,
                    error=str(e),
                )

    async def _alert_corporate_action(
        self: "TradingBot", symbol: str, ratio: float,
    ) -> None:
        """Throttled Discord alert for a suspected split/merge (once/6h).

        Trading is already suppressed in the strategy; this tells a human to
        verify KIS adjusted-price re-fetch and restart the bot so the RSI
        base reloads on the post-split scale.
        """
        now = datetime.now()
        last = self._corp_action_alerted.get(symbol)
        if last is not None and (now - last) < timedelta(hours=6):
            return
        self._corp_action_alerted[symbol] = now
        logger.warning(
            f"[{symbol}] Suspected corporate action (split/merge), step "
            f"ratio {ratio:.4g} — auto-trading suppressed for this symbol"
        )
        if self.notifier:
            try:
                await self.notifier.notify_error(
                    f"{symbol}: 분할/합병 의심 (가격 단절 비율 {ratio:.4g}). "
                    f"자동매매 억제 중.",
                    context=(
                        "corp-action guard — 수정주가 재조회를 위해 봇 재시작 후 "
                        "베이스가 분할 후 스케일인지 검증 필요"
                    ),
                )
            except Exception as exc:
                logger.debug(f"corp-action alert failed: {exc}")
