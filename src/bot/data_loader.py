"""Data loading utilities for trading bot"""

from typing import TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from src.bot.core import TradingBot

logger = logging.getLogger(__name__)


class DataLoaderMixin:
    """Mixin for data loading methods"""

    async def update_initial_rsi(self: "TradingBot") -> None:
        """Calculate and update initial RSI values from loaded historical data"""
        logger.info("Calculating initial RSI values...")

        self.dashboard.rsi_values.clear()
        self.dashboard.rsi_prices.clear()

        for strategy in self.strategy_engine.get_strategies():
            for symbol in strategy.symbols:
                try:
                    if hasattr(strategy, "get_current_rsi"):
                        if (rsi := strategy.get_current_rsi(symbol)) is not None:
                            df = strategy.get_dataframe(symbol)
                            price = float(df["close"].iloc[-1]) if len(df) > 0 else None
                            market = strategy.market.value.upper()

                            self.dashboard.update_rsi(
                                symbol, rsi, price=price, market=market
                            )
                            logger.info(f"  [{symbol}] RSI: {rsi:.1f}")
                except Exception as e:
                    logger.error(f"Failed to calculate RSI for {symbol}: {e}")

    async def load_chart_history(self: "TradingBot") -> None:
        """Load price/RSI history from database on startup (enabled only)"""
        try:
            # Only load history for enabled symbols
            configs = (
                await self.storage.get_all_strategy_configs()
            )
            enabled_symbols = set()
            for cfg in configs:
                if not cfg["enabled"]:
                    continue
                for s in cfg["symbols"]:
                    sym = (
                        s["symbol"]
                        if isinstance(s, dict) else s
                    )
                    enabled_symbols.add(sym)

            all_symbols = await self.storage.get_all_symbols_with_history()
            symbols = [s for s in all_symbols if s in enabled_symbols]
            logger.info(f"Loading chart history for {len(symbols)} symbols...")

            for symbol in symbols:
                history = await self.storage.get_price_rsi_history(symbol, limit=2000)

                for record in history:
                    if record["rsi"] is not None:
                        self.dashboard.add_price_point(
                            symbol=record["symbol"],
                            time=record["timestamp"],
                            open_=record["price"],
                            high=record["price"],
                            low=record["price"],
                            close=record["price"],
                        )
                        self.dashboard.add_rsi_point(
                            symbol=record["symbol"],
                            time=record["timestamp"],
                            rsi=record["rsi"],
                        )

                if history:
                    latest = history[-1]
                    if latest["rsi"] is not None:
                        self.dashboard.update_rsi(
                            symbol,
                            latest["rsi"],
                            price=latest.get("price"),
                            market=latest.get("market"),
                        )
                    logger.info(f"  [{symbol}] Loaded {len(history)} history points")

            logger.info("Chart history loaded successfully")
        except Exception as e:
            logger.warning(f"Failed to load chart history: {e}")

    async def load_equity_history(self: "TradingBot") -> None:
        """Load equity history from database for equity curve chart"""
        try:
            history = await self.storage.get_equity_history(days=90)
            self.dashboard.equity_history = history
            logger.info(f"Loaded {len(history)} equity history points")
        except Exception as e:
            logger.warning(f"Failed to load equity history: {e}")

    async def load_fills(self: "TradingBot") -> None:
        """Load fills from database for performance calculation and trade logs"""
        try:
            fills = await self.storage.get_all_fills()
            self.dashboard.fills = [
                {
                    "order_id": f.order_id,
                    "symbol": f.symbol,
                    "market": f.market.value if f.market else None,
                    "side": f.side.value if f.side else None,
                    "quantity": f.quantity,
                    "price": float(f.price),
                    "commission": float(f.commission),
                    "pnl": float(f.pnl) if f.pnl else None,
                    "timestamp": f.timestamp.isoformat() if f.timestamp else None,
                }
                for f in fills
            ]

            # Populate trade_logs from fills for Trades page
            for f in fills:
                side_val = f.side.value if f.side else "buy"
                action = "buy" if side_val == "buy" else "sell"
                pnl = float(f.pnl) if f.pnl else None
                cost = float(f.price) * f.quantity
                pnl_pct = (pnl / cost * 100) if pnl and cost > 0 else None
                self.dashboard.add_trade_log(
                    symbol=f.symbol,
                    market=f.market.value.upper() if f.market else "US",
                    action=action,
                    price=float(f.price),
                    quantity=f.quantity,
                    rsi=f.rsi,
                    trigger_rule="historical",
                    result="success",
                    pnl=pnl,
                    pnl_pct=round(pnl_pct, 2) if pnl_pct else None,
                )

            self.dashboard.calculate_performance()
            logger.info(f"Loaded {len(fills)} fills, performance calculated")
        except Exception as e:
            logger.warning(f"Failed to load fills: {e}")
