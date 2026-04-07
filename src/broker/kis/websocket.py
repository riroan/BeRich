import asyncio
import json
import websockets
from typing import Optional, AsyncIterator
from datetime import datetime
from decimal import Decimal
import logging

from src.core.types import Market, Quote
from src.core.events import EventBus
from .auth import KISAuth

logger = logging.getLogger(__name__)


class KISWebSocket:
    """KIS WebSocket client for real-time data"""

    WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
    WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"

    def __init__(
        self,
        auth: KISAuth,
        event_bus: EventBus,
        base_url: str,
        paper_trading: bool = True,
    ):
        self._auth = auth
        self._event_bus = event_bus
        self._base_url = base_url
        self._paper_trading = paper_trading
        self._ws_url = self.WS_URL_PAPER if paper_trading else self.WS_URL_REAL

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._subscriptions: set[str] = set()
        self._approval_key: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, session) -> None:
        """Connect to WebSocket"""
        if self._connected:
            return

        # Get approval key
        self._approval_key = await self._auth.get_websocket_key(session)

        # Connect to WebSocket
        self._ws = await websockets.connect(self._ws_url, ping_interval=30)
        self._connected = True
        logger.info(f"WebSocket connected: {self._ws_url}")

    async def disconnect(self) -> None:
        """Disconnect from WebSocket"""
        if self._ws:
            await self._ws.close()
        self._connected = False
        self._subscriptions.clear()
        logger.info("WebSocket disconnected")

    async def subscribe(self, symbol: str, market: Market) -> None:
        """Subscribe to real-time quotes for a symbol"""
        if not self._connected:
            raise Exception("WebSocket not connected")

        sub_key = f"{market.value}:{symbol}"
        if sub_key in self._subscriptions:
            return

        if market == Market.KRX:
            await self._subscribe_domestic(symbol)
        else:
            await self._subscribe_overseas(symbol, market)

        self._subscriptions.add(sub_key)
        logger.info(f"Subscribed to {symbol} ({market.value})")

    async def _subscribe_domestic(self, symbol: str) -> None:
        """Subscribe to domestic stock quotes"""
        message = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",  # 1: subscribe, 2: unsubscribe
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": "H0STCNT0",  # Real-time execution
                    "tr_key": symbol,
                }
            },
        }
        await self._ws.send(json.dumps(message))

    async def _subscribe_overseas(self, symbol: str, market: Market) -> None:
        """Subscribe to overseas stock quotes"""
        exchange_map = {
            Market.NYSE: "NYS",
            Market.NASDAQ: "NAS",
            Market.AMEX: "AMS",
        }

        message = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": "HDFSCNT0",  # Overseas real-time
                    "tr_key": f"{exchange_map.get(market, 'NAS')}{symbol}",
                }
            },
        }
        await self._ws.send(json.dumps(message))

    async def unsubscribe(self, symbol: str, market: Market) -> None:
        """Unsubscribe from real-time quotes"""
        sub_key = f"{market.value}:{symbol}"
        if sub_key not in self._subscriptions:
            return

        if market == Market.KRX:
            message = {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": "2",  # unsubscribe
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": "H0STCNT0", "tr_key": symbol}},
            }
        else:
            exchange_map = {
                Market.NYSE: "NYS",
                Market.NASDAQ: "NAS",
                Market.AMEX: "AMS",
            }
            message = {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": "2",
                    "content-type": "utf-8",
                },
                "body": {
                    "input": {
                        "tr_id": "HDFSCNT0",
                        "tr_key": f"{exchange_map.get(market, 'NAS')}{symbol}",
                    }
                },
            }

        await self._ws.send(json.dumps(message))
        self._subscriptions.discard(sub_key)

    async def receive(self) -> AsyncIterator[Quote]:
        """Receive real-time quotes"""
        if not self._connected:
            raise Exception("WebSocket not connected")

        while self._connected:
            try:
                message = await asyncio.wait_for(self._ws.recv(), timeout=30.0)
                quote = self._parse_message(message)
                if quote:
                    yield quote
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                continue
            except websockets.ConnectionClosed:
                logger.warning("WebSocket connection closed")
                self._connected = False
                break

    def _parse_message(self, message: str) -> Optional[Quote]:
        """Parse WebSocket message to Quote"""
        try:
            # KIS sends data in pipe-separated format
            if message.startswith("{"):
                # JSON response (subscription confirmation, etc.)
                data = json.loads(message)
                if data.get("header", {}).get("tr_id") in ["PINGPONG"]:
                    return None
                return None

            # Parse pipe-separated data
            parts = message.split("|")
            if len(parts) < 4:
                return None

            tr_id = parts[1]
            data = parts[3]

            if tr_id == "H0STCNT0":
                # Domestic stock execution data
                return self._parse_domestic_quote(data)
            elif tr_id == "HDFSCNT0":
                # Overseas stock execution data
                return self._parse_overseas_quote(data)

            return None

        except Exception as e:
            logger.debug(f"Failed to parse message: {e}")
            return None

    def _parse_domestic_quote(self, data: str) -> Optional[Quote]:
        """Parse domestic stock quote data"""
        try:
            fields = data.split("^")
            if len(fields) < 20:
                return None

            return Quote(
                symbol=fields[0],
                market=Market.KRX,
                last_price=Decimal(fields[2]),
                last_size=int(fields[12]),
                bid_price=Decimal(fields[2]),  # Simplified
                ask_price=Decimal(fields[2]),  # Simplified
                bid_size=0,
                ask_size=0,
                timestamp=datetime.now(),
            )
        except Exception:
            return None

    def _parse_overseas_quote(self, data: str) -> Optional[Quote]:
        """Parse overseas stock quote data"""
        try:
            fields = data.split("^")
            if len(fields) < 10:
                return None

            symbol = fields[0]
            # Remove exchange prefix if present
            if len(symbol) > 3 and symbol[:3] in ["NYS", "NAS", "AMS"]:
                symbol = symbol[3:]

            return Quote(
                symbol=symbol,
                market=Market.NASDAQ,  # Default, would need to track
                last_price=Decimal(fields[2]),
                last_size=int(fields[7]) if fields[7] else 0,
                bid_price=Decimal(fields[2]),
                ask_price=Decimal(fields[2]),
                bid_size=0,
                ask_size=0,
                timestamp=datetime.now(),
            )
        except Exception:
            return None
