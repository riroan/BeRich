"""Tests for execution-basis (체결기준) USD account value."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.broker.kis.client import KISBroker

# Real CTRP6504R response shape, from the 2026-08-05 production account.
_PROD_RESPONSE = {
    "rt_cd": "0",
    "output1": [],
    "output2": [
        {
            "crcy_cd": "USD",
            "frcr_dncl_amt_2": "903.170000",
            "frcr_sll_amt_smtl": "1493.840000",
            "frcr_buy_amt_smtl": "435.990000",
            "frcr_drwg_psbl_amt_1": "903.170000",
        }
    ],
    "output3": {"evlu_amt_smtl": "5567", "evlu_pfls_amt_smtl": "107"},
}


def _broker_returning(payload: dict) -> KISBroker:
    broker = KISBroker(
        event_bus=AsyncMock(),
        app_key="k",
        app_secret="s",
        account_no="12345678-01",
        paper_trading=False,
    )
    broker._auth.get_headers = MagicMock(return_value={})
    resp = MagicMock()
    resp.json = AsyncMock(return_value=payload)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    broker._session = MagicMock()
    broker._session.get = MagicMock(return_value=ctx)
    return broker


@pytest.mark.asyncio
async def test_total_eval_includes_unsettled_but_cash_does_not():
    balance = await _broker_returning(_PROD_RESPONSE)._get_overseas_balance()

    # Unsettled = pending sells - pending buys.
    assert balance["unsettled"] == Decimal("1057.850000")
    # Cash stays settlement basis — this is what order sizing caps on.
    assert balance["cash"] == Decimal("903.170000")
    # Account value is execution basis: cash + stock + unsettled.
    assert balance["total_eval"] == Decimal("7528.020000")


@pytest.mark.asyncio
async def test_unsettled_is_zero_when_broker_omits_the_fields():
    # Paper trading (VTRP6504R) can omit or blank these fields; account value
    # must fall back to plain cash + stock rather than blowing up.
    payload = dict(_PROD_RESPONSE)
    payload["output2"] = [
        {"crcy_cd": "USD", "frcr_dncl_amt_2": "903.17", "frcr_sll_amt_smtl": ""}
    ]

    balance = await _broker_returning(payload)._get_overseas_balance()

    assert balance["unsettled"] == Decimal("0")
    assert balance["total_eval"] == Decimal("6470.17")
