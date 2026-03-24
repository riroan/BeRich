"""Discord notification system"""

import asyncio
import aiohttp
from datetime import datetime
from decimal import Decimal
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Discord webhook notifier for trading alerts"""

    def __init__(self, webhook_url: Optional[str] = None, enabled: bool = True):
        self.webhook_url = webhook_url
        self.enabled = enabled and bool(webhook_url)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the session"""
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, message: str, color: int = 0x3498DB) -> bool:
        """Send a message to Discord

        Args:
            message: Message content
            color: Embed color (hex)

        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False

        try:
            session = await self._get_session()

            embed = {
                "description": message,
                "color": color,
                "timestamp": datetime.utcnow().isoformat(),
                "footer": {"text": "BeRich Trading Bot"},
            }

            payload = {"embeds": [embed]}

            async with session.post(self.webhook_url, json=payload) as resp:
                if resp.status == 204:
                    logger.debug("Discord notification sent")
                    return True
                else:
                    text = await resp.text()
                    logger.warning(f"Discord notification failed: {resp.status} {text}")
                    return False

        except Exception as e:
            logger.error(f"Failed to send Discord notification: {e}")
            return False

    async def notify_buy_signal(
        self,
        symbol: str,
        price: Decimal,
        quantity: int,
        rsi: float,
        stage: int,
        total_stages: int,
    ) -> bool:
        """Notify buy signal"""
        value = price * quantity
        message = (
            f"**BUY SIGNAL**\n"
            f"```\n"
            f"Symbol   : {symbol}\n"
            f"Price    : {price:,.0f}\n"
            f"Quantity : {quantity:,}\n"
            f"Value    : {value:,.0f}\n"
            f"RSI      : {rsi:.1f}\n"
            f"Stage    : {stage}/{total_stages}\n"
            f"```"
        )
        return await self.send(message, color=0x2ECC71)  # Green

    async def notify_sell_signal(
        self,
        symbol: str,
        price: Decimal,
        quantity: int,
        rsi: float,
        pnl_pct: float,
        reason: str,
    ) -> bool:
        """Notify sell signal"""
        value = price * quantity
        emoji = "+" if pnl_pct >= 0 else ""
        message = (
            f"**SELL SIGNAL**\n"
            f"```\n"
            f"Symbol   : {symbol}\n"
            f"Price    : {price:,.0f}\n"
            f"Quantity : {quantity:,}\n"
            f"Value    : {value:,.0f}\n"
            f"RSI      : {rsi:.1f}\n"
            f"PnL      : {emoji}{pnl_pct:.1f}%\n"
            f"Reason   : {reason}\n"
            f"```"
        )
        # Red for loss, Orange for stop loss, Blue for profit taking
        if "stop_loss" in reason:
            color = 0xE74C3C  # Red
        elif pnl_pct < 0:
            color = 0xE67E22  # Orange
        else:
            color = 0x3498DB  # Blue

        return await self.send(message, color=color)

    async def notify_order_submitted(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        quantity: int,
    ) -> bool:
        """Notify order submitted"""
        value = price * quantity
        emoji = "BUY" if side.lower() == "buy" else "SELL"
        message = (
            f"**ORDER SUBMITTED**\n"
            f"```\n"
            f"Action   : {emoji}\n"
            f"Symbol   : {symbol}\n"
            f"Price    : {price:,.0f}\n"
            f"Quantity : {quantity:,}\n"
            f"Value    : {value:,.0f}\n"
            f"```"
        )
        color = 0x2ECC71 if side.lower() == "buy" else 0xE74C3C
        return await self.send(message, color=color)

    async def notify_order_filled(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        quantity: int,
        pnl: Optional[Decimal] = None,
    ) -> bool:
        """Notify order filled"""
        value = price * quantity
        emoji = "BUY" if side.lower() == "buy" else "SELL"
        pnl_str = f"\nPnL      : {pnl:+,.0f}" if pnl is not None else ""
        message = (
            f"**ORDER FILLED**\n"
            f"```\n"
            f"Action   : {emoji}\n"
            f"Symbol   : {symbol}\n"
            f"Price    : {price:,.0f}\n"
            f"Quantity : {quantity:,}\n"
            f"Value    : {value:,.0f}{pnl_str}\n"
            f"```"
        )
        color = 0x2ECC71 if side.lower() == "buy" else 0xE74C3C
        return await self.send(message, color=color)

    async def notify_error(self, error: str, context: str = "") -> bool:
        """Notify error"""
        message = (
            f"**ERROR**\n"
            f"```\n"
            f"Context: {context}\n"
            f"Error  : {error}\n"
            f"```"
        )
        return await self.send(message, color=0xE74C3C)  # Red

    async def notify_startup(self, strategies: list[str], paper_trading: bool) -> bool:
        """Notify bot startup"""
        mode = "PAPER" if paper_trading else "REAL"
        strategy_list = "\n".join(f"  - {s}" for s in strategies)
        message = (
            f"**BOT STARTED**\n"
            f"```\n"
            f"Mode       : {mode}\n"
            f"Strategies :\n{strategy_list}\n"
            f"```"
        )
        return await self.send(message, color=0x9B59B6)  # Purple

    async def notify_shutdown(self) -> bool:
        """Notify bot shutdown"""
        message = "**BOT STOPPED**"
        return await self.send(message, color=0x95A5A6)  # Gray
