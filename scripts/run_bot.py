#!/usr/bin/env python3
"""Main entry point for trading bot"""

import asyncio
import signal
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.bot import TradingBot
from src.utils.logger import setup_logger

logger = setup_logger("TradingBot")


def run_web_server(host: str, port: int):
    """Run web server in a separate thread"""
    import uvicorn
    from src.web.app import create_app

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="warning")


async def main():
    """Main entry point"""
    import argparse
    import threading

    parser = argparse.ArgumentParser(description="Trading Bot")
    parser.add_argument(
        "--warmup",
        type=int,
        default=0,
        help="Warmup period in hours before trading starts (default: 0)",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Enable web dashboard",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Web dashboard port (default: 8080)",
    )
    args = parser.parse_args()

    # Start web server in background thread
    if args.web:
        web_thread = threading.Thread(
            target=run_web_server,
            args=("0.0.0.0", args.web_port),
            daemon=True,
        )
        web_thread.start()
        logger.info(f"Web dashboard started at http://localhost:{args.web_port}")

    bot = TradingBot(warmup_hours=args.warmup)

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(bot.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
