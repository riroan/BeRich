"""run_bot.py serves the dashboard's WebSocket in its own thread/loop,
separate from the bot's loop that calls broadcast_update() every tick.
Awaiting a foreign-loop WebSocket send directly raises "Future attached to
a different loop" (see TODO.md's identical diagnosis for the symbol-sync
hot-reload bug) — silently caught, which prunes the connection as
"disconnected" after the very first tick. The dashboard then never
updates again, which is why P&L looked frozen instead of real-time.
"""

import asyncio
import threading

import pytest

from src.web.app import ConnectionManager


class _LoopBoundWS:
    """Stand-in for Starlette's WebSocket: send_text awaits a primitive
    bound to the loop it was created on, exactly like the real ASGI send
    machinery."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.received: list[str] = []

    async def send_text(self, text: str) -> None:
        fut = self.loop.create_future()
        # A real-time delay (not call_soon_threadsafe) guarantees the
        # future is still pending when awaited below, so this always
        # actually suspends instead of racing to resolve first — a
        # cross-loop await only raises on a future that's genuinely
        # pending at suspend time.
        self.loop.call_later(0.05, fut.set_result, None)
        await fut
        self.received.append(text)


@pytest.mark.asyncio
async def test_broadcast_reaches_a_connection_owned_by_another_threads_loop():
    manager = ConnectionManager()
    ws_holder: dict = {}
    ready = threading.Event()

    def web_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def connect():
            ws = _LoopBoundWS()
            ws_holder["ws"] = ws
            manager.active_connections.append(ws)
            manager.loop = loop
            ready.set()

        loop.run_until_complete(connect())
        loop.run_forever()
        loop.close()

    t = threading.Thread(target=web_thread, daemon=True)
    t.start()
    ready.wait(timeout=2)

    try:
        # Called from a DIFFERENT loop than the connection's own, just
        # like the bot's on_tick loop calling broadcast_update().
        await manager.broadcast({"type": "tick", "data": {"pnl_usd": 42}})

        ws = ws_holder["ws"]
        assert ws.received, "message never reached the client's WebSocket loop"
        assert '"pnl_usd": 42' in ws.received[0]
    finally:
        manager.loop.call_soon_threadsafe(manager.loop.stop)
        t.join(timeout=2)
