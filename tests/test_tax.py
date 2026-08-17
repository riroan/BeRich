"""Capital gains tax on realized fills"""

from datetime import date, datetime

from src.analytics.tax import (
    DEDUCTION_KRW,
    TAX_RATE,
    capital_gains_tax,
    capital_gains_tax_by_year,
)

RATE = 1000.0  # round USD/KRW so the expected KRW amounts stay readable


def _fill(pnl, year=2026, market="nasdaq"):
    return {
        "market": market,
        "pnl": pnl,
        "timestamp": f"{year}-06-01T12:00:00",
    }


def _rates(*years):
    """Flat USD/KRW covering every day of the given years."""
    years = years or (2024, 2025, 2026, datetime.now().year)
    return {
        date(y, m, d): RATE
        for y in years
        for m in range(1, 13)
        for d in range(1, 29)
    }


class TestCapitalGainsTax:
    def test_under_deduction_owes_nothing(self):
        result = capital_gains_tax([_fill(1000.0)], 2026, _rates())
        assert result["realized_pnl"] == 1_000_000.0
        assert result["tax"] == 0.0
        assert result["deduction_left"] == DEDUCTION_KRW - 1_000_000.0

    def test_over_deduction_taxes_the_excess_only(self):
        # 3,000 USD * 1,000 = 3,000,000 KRW, 500,000 over the deduction
        result = capital_gains_tax([_fill(3000.0)], 2026, _rates())
        assert result["taxable"] == 500_000.0
        assert result["tax"] == 500_000.0 * TAX_RATE
        assert result["deduction_left"] == 0.0

    def test_losses_net_against_gains(self):
        fills = [_fill(3000.0), _fill(-2000.0)]
        result = capital_gains_tax(fills, 2026, _rates())
        assert result["realized_pnl"] == 1_000_000.0
        assert result["tax"] == 0.0

    def test_losing_year_does_not_grow_the_deduction(self):
        """No carry-over: the allowance is capped at the annual amount."""
        result = capital_gains_tax([_fill(-800.0)], 2026, _rates())
        assert result["realized_pnl"] == -800_000.0
        assert result["deduction_left"] == DEDUCTION_KRW
        assert result["tax"] == 0.0

    def test_krx_is_not_taxed(self):
        fills = [_fill(9999.0, market="krx")]
        result = capital_gains_tax(fills, 2026, _rates())
        assert result["realized_pnl"] == 0.0
        assert result["tax"] == 0.0

    def test_other_years_and_open_positions_excluded(self):
        fills = [
            _fill(9999.0, year=2025),
            {"market": "nasdaq", "pnl": None, "timestamp": "2026-06-01T12:00:00"},
        ]
        result = capital_gains_tax(fills, 2026, _rates())
        assert result["realized_pnl"] == 0.0
        assert result["deduction_left"] == DEDUCTION_KRW

    def test_bad_timestamp_is_skipped(self):
        fills = [{"market": "nasdaq", "pnl": 9999.0, "timestamp": "not-a-date"}]
        assert capital_gains_tax(fills, 2026, _rates())["realized_pnl"] == 0.0

    def test_each_fill_uses_its_own_day_rate(self):
        """The whole point of the FX series: a fill converts at its own date."""
        rates = {date(2026, 1, 15): 1000.0, date(2026, 7, 15): 2000.0}
        fills = [
            {"market": "nasdaq", "pnl": 100.0, "timestamp": "2026-01-15T12:00:00"},
            {"market": "nasdaq", "pnl": 100.0, "timestamp": "2026-07-15T12:00:00"},
        ]
        result = capital_gains_tax(fills, 2026, rates)
        assert result["realized_pnl"] == 300_000.0
        # equal-sized fills at 1,000 and 2,000 average to 1,500
        assert result["fx_rate"] == 1500.0

    def test_fx_rate_is_weighted_by_fill_size(self):
        rates = {date(2026, 1, 15): 1000.0, date(2026, 7, 15): 2000.0}
        fills = [
            {"market": "nasdaq", "pnl": 300.0, "timestamp": "2026-01-15T12:00:00"},
            {"market": "nasdaq", "pnl": 100.0, "timestamp": "2026-07-15T12:00:00"},
        ]
        # (300*1000 + 100*2000) / 400 = 1,250
        assert capital_gains_tax(fills, 2026, rates)["fx_rate"] == 1250.0

    def test_fx_rate_falls_back_to_year_end_when_usd_cancels_out(self):
        rates = {
            date(2026, 1, 15): 1000.0,
            date(2026, 7, 15): 2000.0,
            date(2026, 12, 31): 1400.0,
        }
        fills = [
            {"market": "nasdaq", "pnl": 100.0, "timestamp": "2026-01-15T12:00:00"},
            {"market": "nasdaq", "pnl": -100.0, "timestamp": "2026-07-15T12:00:00"},
        ]
        result = capital_gains_tax(fills, 2026, rates)
        # zero USD leaves no weighted average, so the year-end rate stands in
        assert result["fx_rate"] == 1400.0
        assert result["realized_pnl"] == -100_000.0
        assert result["realized_pnl_usd"] == 0.0

    def test_usd_display_round_trips_the_krw_assessment(self):
        result = capital_gains_tax([_fill(3000.0)], 2026, _rates())
        # KRW is the assessment; USD is that divided by the applied rate
        assert result["realized_pnl_usd"] == 3000.0
        assert result["tax_usd"] == result["tax"] / result["fx_rate"]
        assert result["deduction_left_usd"] == (
            result["deduction_left"] / result["fx_rate"]
        )

    def test_no_rates_reports_unavailable_instead_of_guessing(self):
        result = capital_gains_tax([_fill(9999.0)], 2026, {})
        assert result["year"] == 2026
        assert result["tax"] is None
        assert result["realized_pnl"] is None


class TestCapitalGainsTaxByYear:
    def test_one_row_per_year_newest_first(self):
        fills = [_fill(100.0, year=2024), _fill(200.0, year=2026)]
        rows = capital_gains_tax_by_year(fills, _rates())
        assert [r["year"] for r in rows] == sorted(
            {2024, 2026, datetime.now().year}, reverse=True
        )

    def test_current_year_shown_even_with_no_fills(self):
        rows = capital_gains_tax_by_year([], _rates())
        assert [r["year"] for r in rows] == [datetime.now().year]
        assert rows[0]["deduction_left"] == DEDUCTION_KRW

    def test_krx_only_year_gets_no_row(self):
        rows = capital_gains_tax_by_year(
            [_fill(5000.0, year=2024, market="krx")], _rates()
        )
        assert 2024 not in [r["year"] for r in rows]
