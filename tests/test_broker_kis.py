"""Tests for KISBroker connect-time task gating"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.broker.kis.client import KISBroker

CLIENT_SESSION = "src.broker.kis.client.aiohttp.ClientSession"


def _make_broker(hts_id: str) -> KISBroker:
    broker = KISBroker(
        event_bus=AsyncMock(),
        app_key="k",
        app_secret="s",
        account_no="12345678-01",
        paper_trading=True,
        hts_id=hts_id,
    )
    broker._auth.authenticate = AsyncMock()
    return broker


async def _idle() -> None:
    await asyncio.sleep(3600)


class TestConnectTaskGating:
    """Poller runs regardless of HTS_ID; WS listener stays gated on it."""

    @pytest.mark.asyncio
    async def test_poller_runs_without_hts_id(self):
        broker = _make_broker(hts_id="")

        with patch.object(broker, "_open_orders_poller", _idle), \
             patch.object(broker, "_execution_listener", _idle), \
             patch(CLIENT_SESSION, return_value=MagicMock()):
            await broker.connect()

        try:
            assert broker._exec_poll_task is not None
            assert not broker._exec_poll_task.done()
            # WS listener must NOT start without an HTS ID
            assert broker._exec_listener_task is None
        finally:
            broker._exec_poll_task.cancel()

    @pytest.mark.asyncio
    async def test_both_run_with_hts_id(self):
        broker = _make_broker(hts_id="ABC12345")

        with patch.object(broker, "_open_orders_poller", _idle), \
             patch.object(broker, "_execution_listener", _idle), \
             patch(CLIENT_SESSION, return_value=MagicMock()):
            await broker.connect()

        try:
            assert broker._exec_poll_task is not None
            assert not broker._exec_poll_task.done()
            assert broker._exec_listener_task is not None
            assert not broker._exec_listener_task.done()
        finally:
            broker._exec_poll_task.cancel()
            broker._exec_listener_task.cancel()
