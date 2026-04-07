from decimal import Decimal
from datetime import datetime

from src.core.types import (
    Market,
    Quote,
    Bar,
    Position,
    OrderType,
    OrderStatus,
)


class KISMapper:
    """Map KIS API responses to internal types"""

    # Market codes
    EXCHANGE_CODES = {
        "KRX": "J",      # Korea
        "NYSE": "NYS",   # NYSE
        "NASDAQ": "NAS", # NASDAQ
        "AMEX": "AMS",   # AMEX
    }

    # Order type codes (domestic)
    ORDER_TYPE_CODES = {
        OrderType.MARKET: "01",  # Market order
        OrderType.LIMIT: "00",   # Limit order
    }

    @staticmethod
    def to_market(exchange_code: str) -> Market:
        """Convert exchange code to Market enum"""
        mapping = {
            "J": Market.KRX,
            "NYS": Market.NYSE,
            "NAS": Market.NASDAQ,
            "AMS": Market.AMEX,
        }
        return mapping.get(exchange_code, Market.KRX)

    @staticmethod
    def map_domestic_quote(data: dict) -> Quote:
        """Map domestic stock quote response"""
        return Quote(
            symbol=data.get("stck_shrn_iscd", ""),
            market=Market.KRX,
            bid_price=Decimal(data.get("bidp1", "0")),
            ask_price=Decimal(data.get("askp1", "0")),
            bid_size=int(data.get("bidp_rsqn1", "0")),
            ask_size=int(data.get("askp_rsqn1", "0")),
            last_price=Decimal(data.get("stck_prpr", "0")),
            last_size=int(data.get("cntg_vol", "0")),
            timestamp=datetime.now(),
        )

    @staticmethod
    def map_overseas_quote(data: dict) -> Quote:
        """Map overseas stock quote response"""
        return Quote(
            symbol=data.get("rsym", "").replace("D", "").replace("N", ""),
            market=Market.NYSE,  # Will be updated based on exchange
            bid_price=Decimal(data.get("bidp", "0")),
            ask_price=Decimal(data.get("askp", "0")),
            bid_size=int(data.get("bidsz", "0")),
            ask_size=int(data.get("asksz", "0")),
            last_price=Decimal(data.get("last", "0")),
            last_size=int(data.get("cvol", "0")),
            timestamp=datetime.now(),
        )

    @staticmethod
    def map_domestic_bar(data: dict, symbol: str) -> Bar:
        """Map domestic stock OHLCV response"""
        return Bar(
            symbol=symbol,
            market=Market.KRX,
            open=Decimal(data.get("stck_oprc", "0")),
            high=Decimal(data.get("stck_hgpr", "0")),
            low=Decimal(data.get("stck_lwpr", "0")),
            close=Decimal(data.get("stck_clpr", "0")),
            volume=int(data.get("acml_vol", "0")),
            timestamp=datetime.strptime(data.get("stck_bsop_date", ""), "%Y%m%d"),
            timeframe="1d",
        )

    @staticmethod
    def map_overseas_bar(data: dict, symbol: str, market: Market) -> Bar:
        """Map overseas stock OHLCV response"""
        return Bar(
            symbol=symbol,
            market=market,
            open=Decimal(data.get("open", "0")),
            high=Decimal(data.get("high", "0")),
            low=Decimal(data.get("low", "0")),
            close=Decimal(data.get("clos", "0")),
            volume=int(data.get("tvol", "0")),
            timestamp=datetime.strptime(data.get("xymd", ""), "%Y%m%d"),
            timeframe="1d",
        )

    @staticmethod
    def map_domestic_position(data: dict) -> Position:
        """Map domestic stock position response"""
        quantity = int(data.get("hldg_qty", "0"))
        avg_price = Decimal(data.get("pchs_avg_pric", "0"))
        current_price = Decimal(data.get("prpr", "0"))
        eval_amt = Decimal(data.get("evlu_amt", "0"))
        purchase_amt = Decimal(data.get("pchs_amt", "0"))

        return Position(
            symbol=data.get("pdno", ""),
            market=Market.KRX,
            quantity=quantity,
            avg_entry_price=avg_price,
            current_price=current_price,
            unrealized_pnl=eval_amt - purchase_amt,
        )

    @staticmethod
    def map_overseas_position(data: dict, market: Market) -> Position:
        """Map overseas stock position response"""
        quantity = int(data.get("ccld_qty", "0") or data.get("ord_qty", "0"))
        avg_price = Decimal(data.get("avg_unpr3", "0") or data.get("pchs_avg_pric", "0"))
        current_price = Decimal(data.get("ovrs_now_pric1", "0") or data.get("now_pric2", "0"))
        eval_pnl = Decimal(data.get("frcr_evlu_pfls_amt", "0") or "0")

        return Position(
            symbol=data.get("ovrs_pdno", "") or data.get("pdno", ""),
            market=market,
            quantity=quantity,
            avg_entry_price=avg_price,
            current_price=current_price,
            unrealized_pnl=eval_pnl,
        )

    @staticmethod
    def map_order_status(status_code: str) -> OrderStatus:
        """Map KIS order status code to OrderStatus enum"""
        mapping = {
            "00": OrderStatus.SUBMITTED,
            "01": OrderStatus.PARTIAL_FILLED,
            "02": OrderStatus.FILLED,
            "03": OrderStatus.CANCELLED,
        }
        return mapping.get(status_code, OrderStatus.PENDING)

    @staticmethod
    def get_domestic_order_type_code(order_type: OrderType) -> str:
        """Get KIS order type code for domestic orders"""
        return KISMapper.ORDER_TYPE_CODES.get(order_type, "00")

    @staticmethod
    def get_exchange_code(market: Market) -> str:
        """Get KIS exchange code"""
        return KISMapper.EXCHANGE_CODES.get(market.name, "J")
