"""Discord notification system"""

import aiohttp
from datetime import datetime
from decimal import Decimal
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Color constants
COLOR_GREEN = 0x2ECC71    # Success, Buy
COLOR_RED = 0xE74C3C      # Error, Stop Loss
COLOR_ORANGE = 0xE67E22   # Warning
COLOR_BLUE = 0x3498DB     # Info, Sell profit
COLOR_PURPLE = 0x9B59B6   # System
COLOR_GRAY = 0x95A5A6     # Neutral
COLOR_YELLOW = 0xF1C40F   # Caution


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
        return await self.send(message, color=COLOR_GRAY)

    # ==================== 1. Trade Execution Notifications ====================

    async def notify_buy_executed(
        self,
        symbol: str,
        price: Decimal,
        quantity: int,
        rsi: float,
        stage: int,
        total_stages: int,
        market: str = "USD",
    ) -> bool:
        """Notify buy order executed"""
        stage_text = f"{stage}차 매수" if stage > 0 else "매수"
        value = price * quantity
        price_fmt = f"${price:,.2f}" if market != "KRX" else f"{price:,.0f}원"
        value_fmt = f"${value:,.2f}" if market != "KRX" else f"{value:,.0f}원"

        message = (
            f"**[{symbol}] {stage_text} 체결**\n"
            f"```\n"
            f"가격     : {price_fmt}\n"
            f"수량     : {quantity:,}주\n"
            f"금액     : {value_fmt}\n"
            f"RSI      : {rsi:.1f}\n"
            f"단계     : {stage}/{total_stages}\n"
            f"```"
        )
        return await self.send(message, color=COLOR_GREEN)

    async def notify_sell_executed(
        self,
        symbol: str,
        price: Decimal,
        quantity: int,
        rsi: float,
        pnl: Decimal,
        pnl_pct: float,
        stage: int,
        total_stages: int,
        is_partial: bool = False,
        market: str = "USD",
    ) -> bool:
        """Notify sell order executed"""
        if is_partial:
            stage_text = f"{stage}차 부분 매도"
        else:
            stage_text = "전량 매도"

        value = price * quantity
        price_fmt = f"${price:,.2f}" if market != "KRX" else f"{price:,.0f}원"
        value_fmt = f"${value:,.2f}" if market != "KRX" else f"{value:,.0f}원"
        pnl_fmt = f"${pnl:+,.2f}" if market != "KRX" else f"{pnl:+,.0f}원"

        message = (
            f"**[{symbol}] {stage_text} 체결**\n"
            f"```\n"
            f"가격     : {price_fmt}\n"
            f"수량     : {quantity:,}주\n"
            f"금액     : {value_fmt}\n"
            f"RSI      : {rsi:.1f}\n"
            f"수익     : {pnl_fmt} ({pnl_pct:+.1f}%)\n"
            f"단계     : {stage}/{total_stages}\n"
            f"```"
        )
        color = COLOR_GREEN if pnl >= 0 else COLOR_ORANGE
        return await self.send(message, color=color)

    async def notify_stop_loss_executed(
        self,
        symbol: str,
        price: Decimal,
        quantity: int,
        pnl: Decimal,
        pnl_pct: float,
        market: str = "USD",
    ) -> bool:
        """Notify stop loss executed - HIGH PRIORITY"""
        value = price * quantity
        price_fmt = f"${price:,.2f}" if market != "KRX" else f"{price:,.0f}원"
        value_fmt = f"${value:,.2f}" if market != "KRX" else f"{value:,.0f}원"
        pnl_fmt = f"${pnl:+,.2f}" if market != "KRX" else f"{pnl:+,.0f}원"

        message = (
            f"🚨 **[{symbol}] 손절 실행** 🚨\n"
            f"```\n"
            f"가격     : {price_fmt}\n"
            f"수량     : {quantity:,}주\n"
            f"금액     : {value_fmt}\n"
            f"손실     : {pnl_fmt} ({pnl_pct:+.1f}%)\n"
            f"```"
        )
        return await self.send(message, color=COLOR_RED)

    # ==================== 2. Risk Alerts ====================

    async def notify_stop_loss_imminent(
        self,
        symbol: str,
        current_pnl_pct: float,
        stop_loss_pct: float,
        distance_pct: float,
    ) -> bool:
        """Notify stop loss is imminent"""
        message = (
            f"⚠️ **[{symbol}] 손절 임박**\n"
            f"```\n"
            f"현재 손익  : {current_pnl_pct:+.1f}%\n"
            f"손절 기준  : {stop_loss_pct:.1f}%\n"
            f"남은 거리  : {distance_pct:.1f}%\n"
            f"```"
        )
        return await self.send(message, color=COLOR_ORANGE)

    async def notify_portfolio_loss_threshold(
        self,
        total_pnl_pct: float,
        threshold_pct: float,
    ) -> bool:
        """Notify portfolio total loss reached threshold"""
        message = (
            f"🚨 **포트폴리오 손실 경고** 🚨\n"
            f"```\n"
            f"전체 손익  : {total_pnl_pct:+.1f}%\n"
            f"경고 기준  : {threshold_pct:.1f}%\n"
            f"```"
        )
        return await self.send(message, color=COLOR_RED)

    async def notify_position_concentration(
        self,
        symbol: str,
        current_ratio: float,
        limit_ratio: float,
    ) -> bool:
        """Notify position concentration exceeded"""
        message = (
            f"⚠️ **[{symbol}] 비중 초과**\n"
            f"```\n"
            f"현재 비중  : {current_ratio:.1f}%\n"
            f"설정 한도  : {limit_ratio:.1f}%\n"
            f"```"
        )
        return await self.send(message, color=COLOR_ORANGE)

    async def notify_consecutive_losses(
        self,
        loss_count: int,
        total_loss: Decimal,
        market: str = "USD",
    ) -> bool:
        """Notify consecutive losses"""
        loss_fmt = f"${total_loss:,.2f}" if market != "KRX" else f"{total_loss:,.0f}원"
        message = (
            f"⚠️ **연속 손실 경고**\n"
            f"```\n"
            f"연속 손실  : {loss_count}회\n"
            f"누적 손실  : {loss_fmt}\n"
            f"```"
        )
        return await self.send(message, color=COLOR_ORANGE)

    async def notify_low_cash_ratio(
        self,
        cash_ratio: float,
        min_ratio: float,
    ) -> bool:
        """Notify cash ratio too low"""
        message = (
            f"⚠️ **현금 비중 부족**\n"
            f"```\n"
            f"현재 비중  : {cash_ratio:.1f}%\n"
            f"최소 권장  : {min_ratio:.1f}%\n"
            f"```"
        )
        return await self.send(message, color=COLOR_YELLOW)

    # ==================== 3. System Alerts ====================

    async def notify_order_failed(
        self,
        symbol: str,
        side: str,
        reason: str,
    ) -> bool:
        """Notify order failed - HIGH PRIORITY"""
        action = "매수" if side.lower() == "buy" else "매도"
        message = (
            f"🚨 **[주문 실패] {symbol} {action}**\n"
            f"```\n"
            f"사유: {reason}\n"
            f"```"
        )
        return await self.send(message, color=COLOR_RED)

    async def notify_data_fetch_failed(
        self,
        symbol: str,
        error: str,
        retry_count: int = 0,
    ) -> bool:
        """Notify price data fetch failed"""
        retry_info = f"\n재시도: {retry_count}회" if retry_count > 0 else ""
        message = (
            f"⚠️ **[시세 오류] {symbol}**\n"
            f"```\n"
            f"오류: {error}{retry_info}\n"
            f"```"
        )
        return await self.send(message, color=COLOR_ORANGE)

    async def notify_data_delay(
        self,
        delay_minutes: int,
    ) -> bool:
        """Notify significant data delay"""
        message = (
            f"🚨 **[시스템 오류] 시세 업데이트 지연**\n"
            f"```\n"
            f"지연 시간: {delay_minutes}분\n"
            f"```"
        )
        return await self.send(message, color=COLOR_RED)

    async def notify_strategy_error(
        self,
        strategy_name: str,
        error: str,
    ) -> bool:
        """Notify strategy execution error"""
        message = (
            f"🚨 **[전략 오류] {strategy_name}**\n"
            f"```\n"
            f"오류: {error}\n"
            f"```"
        )
        return await self.send(message, color=COLOR_RED)

    async def notify_auth_expired(self) -> bool:
        """Notify API authentication expired - HIGH PRIORITY"""
        message = (
            "🚨 **[API 오류] 인증 토큰 만료**\n"
            "```\n"
            "API 재인증이 필요합니다.\n"
            "```"
        )
        return await self.send(message, color=COLOR_RED)

    async def notify_account_error(
        self,
        error: str,
    ) -> bool:
        """Notify account query failed"""
        message = (
            f"🚨 **[계좌 오류] 조회 실패**\n"
            f"```\n"
            f"오류: {error}\n"
            f"```"
        )
        return await self.send(message, color=COLOR_RED)

    async def notify_scheduler_stopped(
        self,
        last_run: Optional[datetime] = None,
    ) -> bool:
        """Notify scheduler not working"""
        last_run_str = last_run.strftime("%Y-%m-%d %H:%M:%S") if last_run else "알 수 없음"
        message = (
            f"🚨 **[시스템 오류] 스케줄러 중단**\n"
            f"```\n"
            f"마지막 실행: {last_run_str}\n"
            f"```"
        )
        return await self.send(message, color=COLOR_RED)

    async def notify_system_recovered(
        self,
        issue: str,
    ) -> bool:
        """Notify system recovered from error"""
        message = (
            f"✅ **[시스템 복구] {issue}**\n"
            f"```\n"
            f"정상 작동 중\n"
            f"```"
        )
        return await self.send(message, color=COLOR_GREEN)
