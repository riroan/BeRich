"""해외주식 양도소득세 개산.

국내주식(KRX)은 일반투자자 비과세라 과세 대상에서 뺀다. 매도 건별 실현손익을
체결일 환율로 원화 환산해 연간 손익통산한 뒤, 기본공제 250만원을 빼고
22%(양도소득세 20% + 지방소득세 2%).

환율은 yfinance의 `KRW=X`(일별 USD/KRW)를 쓴다. 계산 자체는 순수 함수로
두고 환율 조회만 분리해서, 세금 로직은 네트워크 없이 테스트된다.
"""

import asyncio
import logging
from datetime import date, datetime
from functools import lru_cache

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

TAX_RATE = 0.22
DEDUCTION_KRW = 2_500_000


def _fill_date(timestamp) -> date | None:
    """Date of a fill timestamp (isoformat str or datetime)."""
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(timestamp, datetime):
        return None
    return timestamp.date()


@lru_cache(maxsize=8)
def _fetch_usdkrw(start_year: int, end_year: int) -> dict[date, float]:
    """Daily USD/KRW, carried onto every calendar day in the range.

    ponytail: cached for the process lifetime, so a long-running server keeps
    the current year's last rate until restart. Fine for an estimate; add a
    TTL if the number drifts visibly.
    """
    df = yf.download(
        "KRW=X",
        start=f"{start_year}-01-01",
        end=f"{end_year + 1}-01-01",
        progress=False,
        auto_adjust=True,
    )
    if df.empty:
        return {}

    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]

    # FX has no weekend or holiday quote but a fill can land on one, so carry
    # the previous session's rate onto every calendar day. Backfill covers a
    # range that opens before the first available quote.
    days = pd.date_range(f"{start_year}-01-01", f"{end_year}-12-31", freq="D")
    close = close.reindex(close.index.union(days)).ffill().bfill().reindex(days)
    return {d.date(): float(v) for d, v in close.items()}


async def usdkrw_rates(fills: list[dict]) -> dict[date, float]:
    """USD/KRW covering every year the fills touch.

    Returns an empty dict when the fetch fails — callers render "no rate"
    rather than a tax figure computed off a guessed rate.
    """
    years = {datetime.now().year}
    for fill in fills:
        filled_on = _fill_date(fill.get("timestamp"))
        if filled_on:
            years.add(filled_on.year)

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_fetch_usdkrw, min(years), max(years)),
            timeout=10.0,
        )
    except Exception as e:
        logger.warning(f"USD/KRW fetch failed, tax shown as unavailable: {e}")
        return {}


def capital_gains_tax(
    fills: list[dict], year: int, rates: dict[date, float]
) -> dict:
    """Estimate one calendar year's overseas capital-gains tax, in KRW.

    Losses net against gains (손익통산). A year that nets out below the
    deduction owes nothing, and `deduction_left` is how much more gain fits
    under it — the number that decides year-end 익절 timing. Every amount is
    None when `rates` is empty, meaning the FX lookup failed.
    """
    if not rates:
        return {
            "year": year,
            "realized_pnl": None,
            "realized_pnl_usd": None,
            "fx_rate": None,
            "deduction_left": None,
            "deduction_left_usd": None,
            "taxable": None,
            "tax": None,
            "tax_usd": None,
        }

    realized = 0.0
    realized_usd = 0.0
    for fill in fills:
        if (fill.get("market") or "").lower() == "krx":
            continue
        pnl = fill.get("pnl")
        filled_on = _fill_date(fill.get("timestamp"))
        if pnl is None or filled_on is None or filled_on.year != year:
            continue
        realized_usd += float(pnl)
        realized += float(pnl) * rates[filled_on]

    taxable = max(realized - DEDUCTION_KRW, 0.0)
    # A losing year does not grow the allowance — the deduction is a fixed
    # annual cap with no carry-over, so only gains consume it.
    used = min(max(realized, 0.0), DEDUCTION_KRW)
    deduction_left = DEDUCTION_KRW - used

    # The rate actually applied, weighted by each fill's size — rates are
    # positive, so this is a true average of them. A year whose gains and
    # losses cancel to zero USD has no such average, and a year with no fills
    # never had one, so both fall back to that year's closing rate. Only the
    # USD display needs this; the tax itself is settled in KRW.
    in_year = [rates[d] for d in sorted(rates) if d.year == year]
    fx_rate = (
        realized / realized_usd if realized_usd
        else (in_year[-1] if in_year else rates[max(rates)])
    )

    return {
        "year": year,
        "realized_pnl": realized,
        "realized_pnl_usd": realized_usd,
        "fx_rate": fx_rate,
        "deduction_left": deduction_left,
        "deduction_left_usd": deduction_left / fx_rate,
        "taxable": taxable,
        "tax": taxable * TAX_RATE,
        "tax_usd": taxable * TAX_RATE / fx_rate,
    }


def capital_gains_tax_by_year(
    fills: list[dict], rates: dict[date, float]
) -> list[dict]:
    """Every year that has a taxable fill, newest first.

    The current year is always included so a fresh account still shows its
    untouched deduction rather than an empty table.
    """
    years = {datetime.now().year}
    for fill in fills:
        if (fill.get("market") or "").lower() == "krx" or fill.get("pnl") is None:
            continue
        filled_on = _fill_date(fill.get("timestamp"))
        if filled_on is not None:
            years.add(filled_on.year)
    return [capital_gains_tax(fills, year, rates) for year in sorted(years, reverse=True)]
