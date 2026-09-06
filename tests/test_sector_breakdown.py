"""Sector aggregation behind /portfolio/sectors."""

from src.web.app import aggregate_by_sector


SYMBOLS = [
    {"symbol": "AAPL", "max_weight": 20.0, "sector": "Technology"},
    {"symbol": "SOXL", "max_weight": 10.0, "sector": "Technology"},
    {"symbol": "CVX", "max_weight": 20.0, "sector": "Energy"},
    {"symbol": "NASA", "max_weight": 50.0, "sector": None},
]
POSITIONS = [
    {"symbol": "AAPL", "quantity": 10, "current_price": 20.0},
    {"symbol": "NASA", "quantity": 5, "current_price": 40.0},
]
FILLS = [
    {"symbol": "AAPL", "pnl": 100.0},
    {"symbol": "SOXL", "pnl": -40.0},
    {"symbol": "CVX", "pnl": None},  # an open buy carries no realized P&L
]


def _by_name(rows):
    return {r["sector"]: r for r in rows}


def test_planned_weight_is_share_of_capped_total():
    rows = _by_name(aggregate_by_sector(SYMBOLS, POSITIONS, FILLS, 1000.0))
    # caps sum to 100 here, so Technology's 30 is 30% of the intent
    assert rows["Technology"]["planned_pct"] == 30.0
    assert rows["Energy"]["planned_pct"] == 20.0
    assert rows["미지정"]["planned_pct"] == 50.0


def test_actual_weight_is_market_value_over_equity():
    rows = _by_name(aggregate_by_sector(SYMBOLS, POSITIONS, FILLS, 1000.0))
    assert rows["Technology"]["actual_pct"] == 20.0  # 10 x 20 / 1000
    assert rows["Energy"]["actual_pct"] == 0.0       # monitored, not held
    assert rows["미지정"]["actual_pct"] == 20.0


def test_realized_pnl_sums_per_sector_and_skips_open_fills():
    rows = _by_name(aggregate_by_sector(SYMBOLS, POSITIONS, FILLS, 1000.0))
    assert rows["Technology"]["realized_pnl"] == 60.0  # 100 - 40
    assert rows["Technology"]["trades"] == 2
    assert rows["Energy"]["realized_pnl"] == 0.0
    assert rows["Energy"]["trades"] == 0  # the pnl-less fill is not a trade


def test_missing_sector_buckets_as_unassigned_and_sorts_last():
    rows = aggregate_by_sector(SYMBOLS, POSITIONS, FILLS, 1000.0)
    # 미지정 has the largest planned weight but still sinks to the bottom
    assert rows[-1]["sector"] == "미지정"
    assert rows[-1]["symbols"] == ["NASA"]


def test_blank_sector_string_counts_as_unassigned():
    rows = _by_name(aggregate_by_sector(
        [{"symbol": "X", "max_weight": 5.0, "sector": "  "}], [], [], 100.0,
    ))
    assert "미지정" in rows


def test_zero_equity_does_not_divide_by_zero():
    rows = aggregate_by_sector(SYMBOLS, POSITIONS, FILLS, 0.0)
    assert all(r["actual_pct"] == 0.0 for r in rows)


def test_stored_sector_outside_the_choice_list_survives():
    """A sector set through the API before it joined SECTOR_CHOICES must still
    be offered, or opening the page and saving would silently rewrite it."""
    from src.web.app import SECTOR_CHOICES

    stored = {"원자재", "정보기술"}
    choices = list(SECTOR_CHOICES) + sorted(stored - set(SECTOR_CHOICES))
    assert "원자재" in choices
    assert choices.count("정보기술") == 1
    # GICS order survives; the stray is appended, not interleaved
    assert choices[:len(SECTOR_CHOICES)] == list(SECTOR_CHOICES)


def test_every_sector_in_use_is_a_valid_choice():
    from src.web.app import SECTOR_CHOICES

    in_use = {
        "정보기술", "금융", "에너지", "산업재",
        "커뮤니케이션 서비스", "경기소비재", "헬스케어",
    }
    assert in_use <= set(SECTOR_CHOICES)
    assert len(SECTOR_CHOICES) == 11
