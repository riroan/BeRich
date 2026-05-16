import aiohttp
import asyncio
from typing import AsyncIterator
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN
import logging

from src.core.types import (
    Market,
    Order,
    Position,
    Quote,
    Bar,
    OrderStatus,
    OrderSide,
)
from src.core.events import EventBus, Event, EventType
from src.core.exceptions import BrokerError, OrderError
from .auth import KISAuth
from .mapper import KISMapper
from .websocket import KISWebSocket

logger = logging.getLogger("TradingBot")


def _format_overseas_price(price: Decimal | None) -> str:
    # KIS overseas tick size: 0.01 for ≥$1, 0.0001 for <$1
    if not price:
        return "0"
    tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    return str(price.quantize(tick, rounding=ROUND_HALF_EVEN))


def _marketable_limit_price(
    price: Decimal, side: OrderSide, buffer: float,
) -> Decimal:
    """Marketable limit: price through the market by ``buffer`` so the
    order fills near-immediately while capping slippage. BUY pays up,
    SELL gives up. Used instead of a raw market order so a stop-loss
    reliably executes without unbounded slippage.
    """
    if side == OrderSide.BUY:
        factor = Decimal(str(1 + buffer))
    else:
        factor = Decimal(str(1 - buffer))
    return price * factor


def _canon_odno(odno: str | None) -> str:
    """KIS ODNO is sometimes zero-padded, sometimes not. Canonicalize
    (strip leading zeros) for matching; keep the raw value for API calls.
    """
    return (odno or "").lstrip("0") or "0"


class KISBroker:
    """Korea Investment & Securities API Client"""

    # API Base URLs
    BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
    BASE_URL_PAPER = "https://openapivts.koreainvestment.com:29443"

    def __init__(
        self,
        event_bus: EventBus,
        app_key: str,
        app_secret: str,
        account_no: str,
        paper_trading: bool = True,
        hts_id: str = "",
        slippage_buffer: float = 0.01,
    ):
        self.event_bus = event_bus
        self.paper_trading = paper_trading
        self._slippage_buffer = slippage_buffer
        self.base_url = self.BASE_URL_PAPER if paper_trading else self.BASE_URL_REAL

        self._auth = KISAuth(
            app_key=app_key,
            app_secret=app_secret,
            account_no=account_no,
            base_url=self.base_url,
        )
        self._session: aiohttp.ClientSession | None = None
        self._websocket: KISWebSocket | None = None
        self._mapper = KISMapper()
        self._connected = False
        self._orders: dict[str, Order] = {}

        self._hts_id = hts_id
        self._exec_listener_task: asyncio.Task | None = None
        self._exec_poll_task: asyncio.Task | None = None
        self._stopping = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def account_no(self) -> str:
        return self._auth.account_no

    async def connect(self) -> None:
        """Connect to KIS API"""
        self._session = aiohttp.ClientSession()
        await self._auth.authenticate(self._session)

        # Initialize WebSocket
        self._websocket = KISWebSocket(
            auth=self._auth,
            event_bus=self.event_bus,
            base_url=self.base_url,
            paper_trading=self.paper_trading,
        )

        self._connected = True
        logger.info(f"KIS broker connected (paper_trading={self.paper_trading})")

        await self.event_bus.publish(
            Event(
                event_type=EventType.BROKER_CONNECTED,
                data={"broker": "KIS", "paper_trading": self.paper_trading},
                timestamp=datetime.now(),
                source="KISBroker",
            )
        )

        # REST polling fallback always runs — it only needs account_no +
        # order_id. Real-time WS execution notices additionally need the
        # HTS ID (used as the subscribe tr_key), so the listener stays gated.
        self._stopping = False
        self._exec_poll_task = asyncio.create_task(
            self._open_orders_poller(),
        )
        if self._hts_id:
            self._exec_listener_task = asyncio.create_task(
                self._execution_listener(),
            )
            logger.info("Execution WS listener + REST poller started")
        else:
            logger.warning(
                "KIS_HTS_ID not set — real-time WS execution notices "
                "disabled; fills detected via 5-min REST polling only",
            )

    async def disconnect(self) -> None:
        """Disconnect from KIS API"""
        self._stopping = True
        for task in (self._exec_listener_task, self._exec_poll_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._exec_listener_task = None
        self._exec_poll_task = None

        if self._websocket:
            await self._websocket.disconnect()
        if self._session:
            await self._session.close()
        self._connected = False
        logger.info("KIS broker disconnected")

    # ==================== Account ====================

    async def get_account_balance(self, market: Market = Market.KRX) -> dict:
        """Get account balance"""
        await self._auth.ensure_authenticated(self._session)

        if market == Market.KRX:
            return await self._retry_on_token_expiry(
                self._get_domestic_balance,
            )
        else:
            return await self._retry_on_token_expiry(
                self._get_overseas_balance,
            )

    async def _get_domestic_balance(self) -> dict:
        """Get domestic stock balance"""
        tr_id = "VTTC8434R" if self.paper_trading else "TTTC8434R"
        endpoint = "/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = self._auth.get_headers(tr_id)
        logger.debug(f"Fetching domestic balance with tr_id={tr_id}")

        params = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}", headers=headers, params=params
        ) as resp:
            data = await resp.json()
            logger.info(f"Domestic balance API response: rt_cd={data.get('rt_cd')}, msg={data.get('msg1')}")

            if data.get("rt_cd") != "0":
                raise BrokerError(f"Failed to get balance: {data.get('msg1')}")

            output2_list = data.get("output2", [])
            output2 = output2_list[0] if output2_list else {}
            logger.info(f"Domestic balance output2: {output2}")
            return {
                "total_eval": Decimal(output2.get("tot_evlu_amt", "0") or "0"),
                "cash": Decimal(output2.get("dnca_tot_amt", "0") or "0"),
                "stocks_eval": Decimal(output2.get("scts_evlu_amt", "0") or "0"),
                "profit_loss": Decimal(output2.get("evlu_pfls_smtl_amt", "0") or "0"),
            }

    async def _get_overseas_balance(self) -> dict:
        """Get overseas stock balance including cash"""
        # Use 해외주식 체결기준현재잔고 API for complete balance info
        tr_id = "VTRP6504R" if self.paper_trading else "CTRP6504R"
        endpoint = "/uapi/overseas-stock/v1/trading/inquire-present-balance"
        headers = self._auth.get_headers(tr_id)

        params = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "WCRC_FRCR_DVSN_CD": "02",  # 02: 외화
            "NATN_CD": "840",  # 미국
            "TR_MKET_CD": "00",  # 전체
            "INQR_DVSN_CD": "00",  # 전체
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}", headers=headers, params=params
        ) as resp:
            data = await resp.json()
            logger.info(f"Overseas balance API response: rt_cd={data.get('rt_cd')}, msg={data.get('msg1')}, keys={list(data.keys())}")

            if data.get("rt_cd") != "0":
                # Fallback to old API if this one fails
                logger.warning(f"Overseas balance API failed, trying fallback: {data.get('msg1')}")
                return await self._get_overseas_balance_fallback()

            output1 = data.get("output1", [])  # Stock positions
            output2 = data.get("output2", [])  # Currency balances
            output3 = data.get("output3", {})  # Summary
            logger.info(f"Overseas balance output1 count: {len(output1)}")
            logger.info(f"Overseas balance output2: {output2}")
            logger.info(f"Overseas balance output3: {output3}")

            # output2 is a list of currency balances, get USD balance
            # frcr_dncl_amt_2: 외화예수금액 (USD cash)
            usd_cash = Decimal("0")
            if output2 and isinstance(output2, list):
                for currency_balance in output2:
                    if currency_balance.get("crcy_cd") == "USD":
                        usd_cash = Decimal(currency_balance.get("frcr_dncl_amt_2", "0") or "0")
                        break

            # output1 has individual stock positions - sum up evaluation amounts
            # evlu_amt: 평가금액 (USD)
            stock_eval = Decimal("0")
            total_profit_loss = Decimal("0")
            for position in output1:
                eval_amt = Decimal(position.get("evlu_amt", "0") or "0")
                pfls_amt = Decimal(position.get("evlu_pfls_amt", "0") or "0")
                stock_eval += eval_amt
                total_profit_loss += pfls_amt

            logger.info(f"Account balance - USD cash: {usd_cash:,.2f}, stocks: {stock_eval:,.2f}")

            return {
                "total_eval": usd_cash + stock_eval,
                "cash": usd_cash,
                "stocks_eval": stock_eval,
                "profit_loss": total_profit_loss,
            }

    async def _get_overseas_balance_fallback(self) -> dict:
        """Fallback overseas balance API"""
        tr_id = "VTTS3012R" if self.paper_trading else "TTTS3012R"
        endpoint = "/uapi/overseas-stock/v1/trading/inquire-balance"
        headers = self._auth.get_headers(tr_id)

        params = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "OVRS_EXCG_CD": "NASD",
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}", headers=headers, params=params
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") != "0":
                return {"total_eval": Decimal("0"), "cash": Decimal("0"), "stocks_eval": Decimal("0"), "profit_loss": Decimal("0")}

            output2 = data.get("output2", {})
            profit_loss = Decimal(output2.get("ovrs_tot_pfls", "0") or "0")

            return {
                "total_eval": Decimal("0"),
                "cash": Decimal("0"),
                "stocks_eval": Decimal("0"),
                "profit_loss": profit_loss,
            }

    # ==================== Positions ====================

    async def get_positions(self, market: Market = Market.KRX) -> list[Position]:
        """Get current positions"""
        await self._auth.ensure_authenticated(self._session)

        if market == Market.KRX:
            return await self._retry_on_token_expiry(
                self._get_domestic_positions,
            )
        else:
            return await self._retry_on_token_expiry(
                self._get_overseas_positions, market,
            )

    async def _get_domestic_positions(self) -> list[Position]:
        """Get domestic stock positions"""
        tr_id = "VTTC8434R" if self.paper_trading else "TTTC8434R"
        endpoint = "/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = self._auth.get_headers(tr_id)

        params = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}", headers=headers, params=params
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") != "0":
                raise BrokerError(f"Failed to get positions: {data.get('msg1')}")

            positions = []
            for item in data.get("output1", []):
                if int(item.get("hldg_qty", "0")) > 0:
                    positions.append(self._mapper.map_domestic_position(item))

            return positions

    async def _get_overseas_positions(self, market: Market) -> list[Position]:
        """Get overseas stock positions"""
        tr_id = "VTTS3012R" if self.paper_trading else "TTTS3012R"
        endpoint = "/uapi/overseas-stock/v1/trading/inquire-balance"
        headers = self._auth.get_headers(tr_id)

        exchange_map = {
            Market.NYSE: "NYSE",
            Market.NASDAQ: "NASD",
            Market.AMEX: "AMEX",
        }

        params = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "OVRS_EXCG_CD": exchange_map.get(market, "NASD"),
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}", headers=headers, params=params
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") != "0":
                raise BrokerError(f"Failed to get overseas positions: {data.get('msg1')}")

            output1 = data.get("output1", []) or []
            summary = [
                {
                    "pdno": i.get("ovrs_pdno") or i.get("pdno"),
                    "cblc": i.get("ovrs_cblc_qty"),
                    "ccld": i.get("ccld_qty"),
                }
                for i in output1
            ]
            logger.info(
                f"Overseas positions [{market.value}] "
                f"count={len(output1)} items={summary}",
            )

            positions = []
            for item in output1:
                qty = int(
                    item.get("ovrs_cblc_qty", "0")
                    or item.get("ccld_qty", "0")
                    or item.get("ord_qty", "0")
                    or "0"
                )
                if qty > 0:
                    positions.append(self._mapper.map_overseas_position(item, market))

            return positions

    # ==================== Orders ====================

    async def submit_order(self, order: Order) -> str:
        """Submit an order"""
        await self._auth.ensure_authenticated(self._session)

        if order.market == Market.KRX:
            return await self._retry_on_token_expiry(
                self._submit_domestic_order, order,
            )
        else:
            return await self._retry_on_token_expiry(
                self._submit_overseas_order, order,
            )

    async def _submit_domestic_order(self, order: Order) -> str:
        """Submit domestic stock order"""
        if order.side == OrderSide.BUY:
            tr_id = "VTTC0802U" if self.paper_trading else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self.paper_trading else "TTTC0801U"

        endpoint = "/uapi/domestic-stock/v1/trading/order-cash"
        headers = self._auth.get_headers(tr_id)

        body = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "PDNO": order.symbol,
            "ORD_DVSN": self._mapper.get_domestic_order_type_code(order.order_type),
            "ORD_QTY": str(order.quantity),
            "ORD_UNPR": str(order.price) if order.price else "0",
        }

        async with self._session.post(
            f"{self.base_url}{endpoint}", headers=headers, json=body
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") == "0":
                order_id = data["output"]["ODNO"]
                order.order_id = order_id
                order.status = OrderStatus.SUBMITTED
                self._orders[order_id] = order

                await self._emit_order_event(order)
                logger.info(f"Order submitted: {order_id} {order.side.value} {order.symbol}")
                return order_id
            else:
                order.status = OrderStatus.REJECTED
                raise OrderError(f"Order rejected: {data.get('msg1')}")

    async def _submit_overseas_order(self, order: Order) -> str:
        """Submit overseas stock order"""
        if order.side == OrderSide.BUY:
            tr_id = "VTTT1002U" if self.paper_trading else "TTTT1002U"
        else:
            tr_id = "VTTT1006U" if self.paper_trading else "TTTT1006U"

        endpoint = "/uapi/overseas-stock/v1/trading/order"
        headers = self._auth.get_headers(tr_id)

        exchange_map = {
            Market.NYSE: "NYSE",
            Market.NASDAQ: "NASD",
            Market.AMEX: "AMEX",
        }

        # Marketable limit: KIS overseas market orders are unreliable
        # (venue/session restricted), so submit a limit priced through
        # the market by _slippage_buffer. Fills near-immediately yet a
        # stop-loss can't slip beyond the buffer.
        if order.price:
            limit_price = _marketable_limit_price(
                order.price, order.side, self._slippage_buffer,
            )
            ord_dvsn = "00"  # limit
        else:
            limit_price = None
            ord_dvsn = "01"  # market (fallback; price unknown)

        body = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "OVRS_EXCG_CD": exchange_map.get(order.market, "NASD"),
            "PDNO": order.symbol,
            "ORD_QTY": str(order.quantity),
            "OVRS_ORD_UNPR": _format_overseas_price(limit_price),
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_dvsn,
        }

        async with self._session.post(
            f"{self.base_url}{endpoint}", headers=headers, json=body
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") == "0":
                order_id = data["output"]["ODNO"]
                order.order_id = order_id
                order.status = OrderStatus.SUBMITTED
                self._orders[order_id] = order

                await self._emit_order_event(order)
                logger.info(f"Overseas order submitted: {order_id}")
                return order_id
            else:
                order.status = OrderStatus.REJECTED
                raise OrderError(f"Overseas order rejected: {data.get('msg1')}")

    async def cancel_order(self, order_id: str, market: Market = Market.KRX) -> bool:
        """Cancel an order"""
        await self._auth.ensure_authenticated(self._session)

        if market == Market.KRX:
            return await self._cancel_domestic_order(order_id)
        else:
            return await self._cancel_overseas_order(order_id, market)

    async def _cancel_domestic_order(self, order_id: str) -> bool:
        """Cancel domestic order"""
        tr_id = "VTTC0803U" if self.paper_trading else "TTTC0803U"
        endpoint = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
        headers = self._auth.get_headers(tr_id)

        if not (order := self._orders.get(order_id)):
            return False

        body = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_id,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(order.quantity - order.filled_quantity),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }

        async with self._session.post(
            f"{self.base_url}{endpoint}", headers=headers, json=body
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") == "0":
                order.status = OrderStatus.CANCELLED
                await self._emit_order_event(order)
                logger.info(f"Order cancelled: {order_id}")
                return True

            return False

    async def _cancel_overseas_order(self, order_id: str, market: Market) -> bool:
        """Cancel overseas order"""
        tr_id = "VTTT1004U" if self.paper_trading else "TTTT1004U"
        endpoint = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
        headers = self._auth.get_headers(tr_id)

        if not (order := self._orders.get(order_id)):
            return False

        exchange_map = {
            Market.NYSE: "NYSE",
            Market.NASDAQ: "NASD",
            Market.AMEX: "AMEX",
        }

        body = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "OVRS_EXCG_CD": exchange_map.get(market, "NASD"),
            "PDNO": order.symbol,
            "ORGN_ODNO": order_id,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(order.quantity - order.filled_quantity),
            "OVRS_ORD_UNPR": "0",
        }

        async with self._session.post(
            f"{self.base_url}{endpoint}", headers=headers, json=body
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") == "0":
                order.status = OrderStatus.CANCELLED
                await self._emit_order_event(order)
                return True

            return False

    # ==================== Market Data ====================

    async def _retry_on_token_expiry(self, func, *args, **kwargs):
        """Retry API call once if token expired"""
        try:
            return await func(*args, **kwargs)
        except BrokerError as e:
            if "만료" in str(e):
                logger.info("Token expired, re-authenticating...")
                self._auth.invalidate()
                await self._auth.ensure_authenticated(self._session)
                return await func(*args, **kwargs)
            raise

    async def get_current_price(self, symbol: str, market: Market = Market.KRX) -> Decimal:
        """Get current price for a symbol"""
        await self._auth.ensure_authenticated(self._session)

        if market == Market.KRX:
            return await self._retry_on_token_expiry(
                self._get_domestic_price, symbol,
            )
        else:
            return await self._retry_on_token_expiry(
                self._get_overseas_price, symbol, market,
            )

    async def _get_domestic_price(self, symbol: str) -> Decimal:
        """Get domestic stock current price"""
        tr_id = "FHKST01010100"
        endpoint = "/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = self._auth.get_headers(tr_id)

        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}", headers=headers, params=params
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") != "0":
                raise BrokerError(f"Failed to get price: {data.get('msg1')}")

            return Decimal(data["output"]["stck_prpr"])

    async def _get_overseas_price(self, symbol: str, market: Market) -> Decimal:
        """Get overseas stock current price"""
        tr_id = "HHDFS00000300"
        endpoint = "/uapi/overseas-price/v1/quotations/price"
        headers = self._auth.get_headers(tr_id)

        exchange_map = {
            Market.NYSE: "NYS",
            Market.NASDAQ: "NAS",
            Market.AMEX: "AMS",
        }

        params = {
            "AUTH": "",
            "EXCD": exchange_map.get(market, "NAS"),
            "SYMB": symbol,
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}", headers=headers, params=params
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") != "0":
                raise BrokerError(f"Failed to get overseas price: {data.get('msg1')}")

            price_str = data["output"].get("last", "")
            if not price_str or price_str == "0":
                logger.warning(f"No price data for {symbol}, API response: {data.get('output', {})}")
                raise BrokerError(f"No price data for {symbol}")

            return Decimal(price_str)

    async def get_historical_bars(
        self,
        symbol: str,
        market: Market = Market.KRX,
        days: int = 100,
    ) -> list[Bar]:
        """Get historical OHLCV data"""
        await self._auth.ensure_authenticated(self._session)

        if market == Market.KRX:
            return await self._retry_on_token_expiry(
                self._get_domestic_bars, symbol, days,
            )
        else:
            return await self._retry_on_token_expiry(
                self._get_overseas_bars, symbol, market, days,
            )

    async def _get_domestic_bars(self, symbol: str, days: int) -> list[Bar]:
        """Get domestic stock historical bars"""
        tr_id = "FHKST01010400"
        endpoint = "/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        headers = self._auth.get_headers(tr_id)

        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}", headers=headers, params=params
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") != "0":
                raise BrokerError(f"Failed to get bars: {data.get('msg1')}")

            bars = []
            for item in data.get("output", [])[:days]:
                try:
                    bars.append(self._mapper.map_domestic_bar(item, symbol))
                except Exception:
                    continue

            return list(reversed(bars))

    async def _get_overseas_bars(
        self, symbol: str, market: Market, days: int
    ) -> list[Bar]:
        """Get overseas stock historical bars"""
        tr_id = "HHDFS76240000"
        endpoint = "/uapi/overseas-price/v1/quotations/dailyprice"
        headers = self._auth.get_headers(tr_id)

        exchange_map = {
            Market.NYSE: "NYS",
            Market.NASDAQ: "NAS",
            Market.AMEX: "AMS",
        }

        params = {
            "AUTH": "",
            "EXCD": exchange_map.get(market, "NAS"),
            "SYMB": symbol,
            "GUBN": "0",
            "BYMD": "",
            "MODP": "1",
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}", headers=headers, params=params
        ) as resp:
            data = await resp.json()

            if data.get("rt_cd") != "0":
                raise BrokerError(f"Failed to get overseas bars: {data.get('msg1')}")

            bars = []
            for item in data.get("output2", [])[:days]:
                try:
                    bars.append(self._mapper.map_overseas_bar(item, symbol, market))
                except Exception:
                    continue

            return list(reversed(bars))

    # ==================== WebSocket ====================

    async def subscribe_quotes(
        self, symbols: list[str], market: Market = Market.KRX
    ) -> AsyncIterator[Quote]:
        """Subscribe to real-time quotes"""
        if not self._websocket:
            raise BrokerError("WebSocket not initialized")

        await self._websocket.connect(self._session)

        for symbol in symbols:
            await self._websocket.subscribe(symbol, market)

        async for quote in self._websocket.receive():
            yield quote

    # ==================== Helpers ====================

    async def _emit_order_event(self, order: Order) -> None:
        """Emit order status event"""
        event_map = {
            OrderStatus.SUBMITTED: EventType.ORDER_SUBMITTED,
            OrderStatus.FILLED: EventType.ORDER_FILLED,
            OrderStatus.PARTIAL_FILLED: EventType.ORDER_PARTIAL_FILLED,
            OrderStatus.CANCELLED: EventType.ORDER_CANCELLED,
            OrderStatus.REJECTED: EventType.ORDER_REJECTED,
        }

        if (event_type := event_map.get(order.status)):
            await self.event_bus.publish(
                Event(
                    event_type=event_type,
                    data=order,
                    timestamp=datetime.now(),
                    source="KISBroker",
                )
            )

    # ============== Execution-notice listener + polling ==============

    async def _execution_listener(self) -> None:
        """Supervisor: hold WS connection open, dispatch execution notices.

        Auto-reconnects with exponential backoff (1s → 60s cap). On every
        successful subscribe the backoff resets. Cancelled cleanly via
        ``disconnect()``.
        """
        backoff = 1.0
        while not self._stopping:
            try:
                await self._websocket.connect(self._session)
                await self._websocket.subscribe_executions(self._hts_id)
                backoff = 1.0
                async for notice in self._websocket.receive_executions():
                    try:
                        await self._apply_execution_notice(notice)
                    except Exception:
                        logger.exception(
                            "Failed to apply execution notice",
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    f"Execution WS listener error: {e!r} "
                    f"(retry in {backoff:.0f}s)",
                )

            # Tear down before retrying to ensure clean state
            try:
                if self._websocket:
                    await self._websocket.disconnect()
            except Exception:
                pass

            if self._stopping:
                break
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, 60.0)

    def _find_order_by_odno(self, odno: str):
        """Match an order by ODNO, tolerant of KIS zero-padding."""
        if (order := self._orders.get(odno)) is not None:
            return order
        target = _canon_odno(odno)
        for oid, o in self._orders.items():
            if _canon_odno(oid) == target:
                return o
        return None

    async def _apply_execution_notice(self, notice) -> None:
        """Translate ExecutionNotice → order.status update + ORDER_FILLED."""
        order = self._find_order_by_odno(notice.order_no)
        if not order:
            logger.debug(
                f"Execution notice for unknown order_no={notice.order_no}; "
                f"likely placed outside the bot",
            )
            return

        if notice.is_rejected:
            order.status = OrderStatus.REJECTED
        elif notice.revise_cancel == "2":
            order.status = OrderStatus.CANCELLED
        elif notice.is_filled and notice.qty > 0:
            order.filled_quantity = (order.filled_quantity or 0) + notice.qty
            order.filled_avg_price = notice.price
            if order.filled_quantity >= order.quantity:
                order.status = OrderStatus.FILLED
            else:
                order.status = OrderStatus.PARTIAL_FILLED
        else:
            # accepted-only (CNTG_YN=N, no fill); nothing to emit
            return

        logger.info(
            f"Order {notice.order_no} {notice.symbol} → "
            f"{order.status.value} (qty={notice.qty}, price={notice.price})",
        )
        await self._emit_order_event(order)

    async def _open_orders_poller(self) -> None:
        """Periodic REST fallback for orders the WS listener may have missed.

        Runs every 5 minutes. Cheaper than the WS path and idempotent — if
        the WS already updated the order, the order won't be in `stuck` and
        the poll skips it.
        """
        POLL_INTERVAL = 300
        while not self._stopping:
            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            try:
                await self._sync_open_orders()
            except Exception as e:
                logger.warning(f"Open orders polling failed: {e!r}")

    async def _sync_open_orders(self) -> None:
        stuck = [
            o for o in self._orders.values()
            if o.status in (
                OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED,
            )
        ]
        if not stuck:
            return
        logger.info(f"Polling KIS for {len(stuck)} open order(s)")
        for order in stuck:
            try:
                if order.market == Market.KRX:
                    # Domestic polling not implemented (KRX strategy disabled)
                    continue
                await self._poll_overseas_order(order)
            except Exception as e:
                logger.warning(
                    f"Poll failed for {order.order_id}: {e!r}",
                )

    async def _poll_overseas_order(self, order: Order) -> None:
        """Live poll: query one open order and emit ORDER_* on change"""
        if await self._query_overseas_fill(order):
            logger.info(
                f"[POLL] Order {order.order_id} → {order.status.value} "
                f"(filled {order.filled_quantity}/{order.quantity} "
                f"@ {order.filled_avg_price})",
            )
            await self._emit_order_event(order)

    async def reconcile_open_orders(
        self, orders: list[Order],
    ) -> list[Order]:
        """One-shot startup reconciliation for orders left open by a
        previous process.

        Historical fills are detected and returned to the caller for
        DB persistence, but NOT emitted as ORDER_FILLED — the strategy
        position is restored independently via sync_positions(), so
        replaying the fill through the event path would double-count.
        Orders that are still genuinely open are registered into the
        live poller set so this session keeps tracking them.
        """
        changed: list[Order] = []
        for order in orders:
            try:
                if order.market == Market.KRX:
                    continue  # domestic polling not implemented
                if await self._query_overseas_fill(order):
                    changed.append(order)
                elif order.order_id:
                    self._orders[order.order_id] = order
            except Exception as e:
                logger.warning(
                    f"Reconcile failed for {order.order_id}: {e!r}",
                )
        if changed:
            logger.info(
                f"Reconciled {len(changed)} stale order(s) to terminal "
                f"state at startup",
            )
        return changed

    async def _query_overseas_fill(self, order: Order) -> bool:
        """Query KIS overseas inquire-ccnl for one order.

        Mutates ``order`` (filled_quantity / filled_avg_price / status)
        in place and returns True iff the order advanced. Pure query —
        emits no event, so it is safe to call during startup
        reconciliation as well as from the live poller.
        """
        tr_id = "VTTS3035R" if self.paper_trading else "TTTS3035R"
        endpoint = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
        headers = self._auth.get_headers(tr_id)

        exchange_map = {
            Market.NYSE: "NYSE",
            Market.NASDAQ: "NASD",
            Market.AMEX: "AMEX",
        }
        today = datetime.now().strftime("%Y%m%d")

        params = {
            "CANO": self._auth.account_no[:8],
            "ACNT_PRDT_CD": self._auth.account_no[9:],
            "PDNO": order.symbol,
            "ORD_STRT_DT": today,
            "ORD_END_DT": today,
            "SLL_BUY_DVSN": "00",
            "CCLD_NCCS_DVSN": "00",
            "OVRS_EXCG_CD": exchange_map.get(order.market, "NASD"),
            "SORT_SQN": "DS",
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": order.order_id or "",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }

        async with self._session.get(
            f"{self.base_url}{endpoint}",
            headers=headers, params=params,
        ) as resp:
            data = await resp.json()

        if data.get("rt_cd") != "0":
            logger.warning(
                f"inquire-ccnl rt_cd={data.get('rt_cd')} "
                f"msg={data.get('msg1')}",
            )
            return False

        for row in data.get("output", []) or []:
            if _canon_odno(row.get("odno")) != _canon_odno(order.order_id):
                continue

            ft_ccld_qty = int(row.get("ft_ccld_qty", "0") or "0")
            nccs_qty = int(row.get("nccs_qty", "0") or "0")
            try:
                ft_ccld_unpr = Decimal(
                    row.get("ft_ccld_unpr3")
                    or row.get("avg_prvs")
                    or "0",
                )
            except Exception:
                ft_ccld_unpr = Decimal("0")

            prev_filled = order.filled_quantity or 0
            if ft_ccld_qty <= prev_filled:
                return False  # nothing new

            order.filled_quantity = ft_ccld_qty
            if ft_ccld_unpr > 0:
                order.filled_avg_price = ft_ccld_unpr
            if nccs_qty == 0 and ft_ccld_qty >= order.quantity:
                order.status = OrderStatus.FILLED
            else:
                order.status = OrderStatus.PARTIAL_FILLED
            return True

        logger.debug(
            f"Order {order.order_id} not in inquire-ccnl response",
        )
        return False
