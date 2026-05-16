import asyncio
import json
import websockets
from dataclasses import dataclass
from typing import AsyncIterator
from datetime import datetime
from decimal import Decimal
import logging

from src.core.types import Market, Quote
from src.core.events import EventBus
from ._crypto import aes_cbc_decrypt
from .auth import KISAuth

logger = logging.getLogger(__name__)


EXECUTION_TR_IDS_REAL = ("H0STCNI0", "H0GSCNI0")
EXECUTION_TR_IDS_PAPER = ("H0STCNI9", "H0GSCNI9")
ALL_EXECUTION_TR_IDS = frozenset(
    EXECUTION_TR_IDS_REAL + EXECUTION_TR_IDS_PAPER
)


@dataclass
class ExecutionNotice:
    """One filled/accepted/rejected event from KIS execution-notice channel"""
    tr_id: str
    order_no: str
    orig_order_no: str
    symbol: str
    side: str               # "buy" | "sell"
    qty: int                # CNTG_QTY — 체결수량 (0 if not filled yet)
    price: Decimal          # CNTG_UNPR
    exec_time: str          # STCK_CNTG_HOUR (HHMMSS)
    is_filled: bool         # CNTG_YN == "Y"
    is_rejected: bool       # RFUS_YN == "Y"
    is_accepted: bool       # ACPT_YN == "Y" (domestic only; overseas absent)
    revise_cancel: str      # RCTF_CLS: "0"=정상 "1"=정정 "2"=취소

    @classmethod
    def parse(cls, tr_id: str, payload: str) -> "ExecutionNotice | None":
        # KIS sometimes packs multiple records separated by '^' boundaries
        # between rows; here we take the first record. Live tests can refine.
        fields = payload.split("^")
        if len(fields) < 13:
            logger.warning(
                f"Execution notice too short for {tr_id}: {len(fields)} fields",
            )
            return None
        try:
            if tr_id in ("H0STCNI0", "H0STCNI9"):
                # Domestic: CUST_ID|ACNT_NO|ODER_NO|OODER_NO|SELN_BYOV_CLS
                # |RCTF_CLS|ODER_KIND|ODER_COND|STCK_SHRN_ISCD|CNTG_QTY
                # |CNTG_UNPR|STCK_CNTG_HOUR|RFUS_YN|CNTG_YN|ACPT_YN|...
                symbol = fields[8]
                qty_str = fields[9]
                price_str = fields[10]
                exec_time = fields[11]
                rfus_yn = fields[12]
                cntg_yn = fields[13] if len(fields) > 13 else "N"
                acpt_yn = fields[14] if len(fields) > 14 else "N"
            else:
                # Overseas: CUST_ID|ACNT_NO|ODER_NO|OODER_NO|SELN_BYOV_CLS
                # |RCTF_CLS|ODER_KIND2|STCK_SHRN_ISCD|CNTG_QTY|CNTG_UNPR
                # |STCK_CNTG_HOUR|RFUS_YN|CNTG_YN|...
                symbol = fields[7]
                qty_str = fields[8]
                price_str = fields[9]
                exec_time = fields[10]
                rfus_yn = fields[11]
                cntg_yn = fields[12]
                acpt_yn = "N"

            side = "sell" if fields[4] in ("01", "1") else "buy"
            return cls(
                tr_id=tr_id,
                order_no=fields[2].lstrip("0") or fields[2],
                orig_order_no=fields[3].lstrip("0") or fields[3],
                symbol=symbol,
                side=side,
                qty=int(qty_str) if qty_str.strip() else 0,
                price=Decimal(price_str) if price_str.strip() else Decimal("0"),
                exec_time=exec_time,
                is_filled=cntg_yn == "Y",
                is_rejected=rfus_yn == "Y",
                is_accepted=acpt_yn == "Y",
                revise_cancel=fields[5],
            )
        except (ValueError, IndexError) as e:
            logger.warning(
                f"Failed to parse execution notice {tr_id}: {e} | {payload[:120]}",
            )
            return None


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

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._connected = False
        self._subscriptions: set[str] = set()
        self._approval_key: str | None = None
        # AES key/iv per execution tr_id (received in subscribe ACK)
        self._cipher: dict[str, tuple[str, str]] = {}

    def execution_tr_ids(self) -> tuple[str, ...]:
        return (
            EXECUTION_TR_IDS_PAPER if self._paper_trading
            else EXECUTION_TR_IDS_REAL
        )

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
        self._cipher.clear()
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

    async def subscribe_executions(self, hts_id: str) -> None:
        """Subscribe to execution-notice channel (domestic + overseas)"""
        if not self._connected:
            raise Exception("WebSocket not connected")
        if not hts_id:
            raise ValueError("hts_id is required for execution notices")

        for tr_id in self.execution_tr_ids():
            if tr_id in self._subscriptions:
                continue
            message = {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": tr_id, "tr_key": hts_id}},
            }
            await self._ws.send(json.dumps(message))
            self._subscriptions.add(tr_id)
            logger.info(f"Subscribing execution channel: {tr_id}")

    async def receive_executions(self) -> AsyncIterator[ExecutionNotice]:
        """Yield ExecutionNotice items as they arrive on the WS connection.

        Handles AES key/iv extraction from subscribe-ACK frames transparently.
        Raises websockets.ConnectionClosed when the WS drops; caller is
        expected to reconnect and resubscribe.
        """
        if not self._connected:
            raise Exception("WebSocket not connected")

        while self._connected:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                self._connected = False
                raise

            if raw.startswith("{"):
                self._handle_json_frame(raw)
                continue

            notice = self._parse_execution_frame(raw)
            if notice:
                yield notice

    def _handle_json_frame(self, raw: str) -> None:
        """Process JSON control frames (ACK, PINGPONG)"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        header = data.get("header", {})
        tr_id = header.get("tr_id", "")

        if tr_id == "PINGPONG":
            # KIS expects the same payload echoed back
            if self._ws:
                asyncio.create_task(self._ws.send(raw))
            return

        if tr_id in ALL_EXECUTION_TR_IDS:
            body = data.get("body", {})
            rt_cd = body.get("rt_cd")
            output = body.get("output") or {}
            key = output.get("key")
            iv = output.get("iv")
            if rt_cd == "0" and key and iv:
                self._cipher[tr_id] = (key, iv)
                logger.info(
                    f"Execution channel ready: {tr_id} (AES key stored)",
                )
            else:
                logger.warning(
                    f"Execution subscribe ACK without key: {tr_id} | "
                    f"rt_cd={rt_cd} msg={body.get('msg1')}",
                )

    def _parse_execution_frame(self, raw: str) -> ExecutionNotice | None:
        # KIS data frame: <encrypt>|<tr_id>|<count>|<payload>
        parts = raw.split("|", 3)
        if len(parts) < 4:
            return None
        encrypt_flag, tr_id, _count, payload = parts

        if tr_id not in ALL_EXECUTION_TR_IDS:
            return None

        if encrypt_flag == "1":
            kv = self._cipher.get(tr_id)
            if not kv:
                logger.warning(
                    f"No AES key for {tr_id}, dropping notice",
                )
                return None
            try:
                payload = aes_cbc_decrypt(kv[0], kv[1], payload)
            except Exception as e:
                logger.warning(f"AES decrypt failed for {tr_id}: {e}")
                return None

        return ExecutionNotice.parse(tr_id, payload)

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

    def _parse_message(self, message: str) -> Quote | None:
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

    def _parse_domestic_quote(self, data: str) -> Quote | None:
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

    def _parse_overseas_quote(self, data: str) -> Quote | None:
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
