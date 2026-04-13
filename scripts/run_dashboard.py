#!/usr/bin/env python3
"""Run web dashboard standalone (for development/testing)"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import os
import uvicorn
import src.web.app as web_app
from src.web.app import create_app, get_dashboard_state

# Create mock data for testing
def setup_mock_data():
    import random
    from datetime import datetime, timedelta

    state = get_dashboard_state()

    # Mock positions
    state.update_position(
        symbol="005930",
        market="KRX",
        quantity=100,
        avg_price=75000,
        current_price=76500,
        rsi=45.2,
    )
    state.update_position(
        symbol="AAPL",
        market="NASDAQ",
        quantity=50,
        avg_price=185.50,
        current_price=192.30,
        rsi=62.8,
    )

    # Mock RSI values
    state.update_rsi("005930", 45.2)
    state.update_rsi("069500", 38.5)
    state.update_rsi("AAPL", 62.8)
    state.update_rsi("NVDA", 71.2)
    state.update_rsi("GOOG", 28.3)

    # Generate mock price history
    symbols = ["005930", "AAPL", "GOOG", "NVDA"]
    base_prices = {"005930": 75000, "AAPL": 190, "GOOG": 145, "NVDA": 850}

    for symbol in symbols:
        base = base_prices.get(symbol, 100)
        price = base

        for i in range(100):
            time = datetime.now() - timedelta(minutes=(100 - i))
            change = random.uniform(-0.02, 0.02)
            price = price * (1 + change)

            open_ = price * (1 + random.uniform(-0.005, 0.005))
            high = max(open_, price) * (1 + random.uniform(0, 0.01))
            low = min(open_, price) * (1 - random.uniform(0, 0.01))

            state.add_price_point(
                symbol=symbol,
                time=time,
                open_=open_,
                high=high,
                low=low,
                close=price,
            )

            # RSI between 20-80 with some trend
            rsi = 50 + random.uniform(-30, 30)
            state.add_rsi_point(symbol, time, rsi)

    # Mock signals
    state.add_signal({
        "type": "ENTRY_LONG",
        "symbol": "GOOG",
        "rsi": 28.3,
    })
    state.add_signal({
        "type": "EXIT_LONG",
        "symbol": "NVDA",
        "rsi": 71.2,
    })

    # Mock orders
    state.add_order({
        "side": "BUY",
        "symbol": "GOOG",
        "quantity": 10,
        "price": 142500,
    })

    # Mock bot status
    from decimal import Decimal
    state.account_value = Decimal("100000000")
    state.balance_krw = Decimal("85000000")
    state.balance_usd = Decimal("12500.50")
    state.cash_krw = Decimal("15000000")
    state.cash_usd = Decimal("3500.25")
    state.pnl_krw = Decimal("1250000")
    state.pnl_usd = Decimal("-125.30")
    state.daily_pnl = Decimal("1250000")
    state.total_pnl = Decimal("5430000")

    state.set_bot_status(
        running=True,
        paper_trading=True,
        strategies=["KRX_RSI_MeanReversion", "NASDAQ_RSI_MeanReversion"],
        uptime="2h 35m",
        warmup_remaining="5h 25m",
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BeRich Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind")
    parser.add_argument("--mock", action="store_true", help="Use mock data")
    args = parser.parse_args()

    # Set db_url so storage-backed API endpoints work
    state = get_dashboard_state()
    state.db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/trading.db")

    if args.mock:
        web_app.MOCK_MODE = True
        setup_mock_data()
        print("Using mock data for testing (auth disabled)")

    app = create_app()
    print(f"Starting dashboard at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
