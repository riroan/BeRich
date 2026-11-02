"""Per-day equity rows served to the 일봉 chart and Monthly Returns."""

from src.web.app import daily_equity_rows, flow_adjusted_series


def test_daily_rows_fold_snapshots_into_ohlc_and_twr():
    points = [
        {"timestamp": "2026-10-01T09:00:00", "total_usd": 100},
        {"timestamp": "2026-10-01T12:00:00", "total_usd": 110},
        {"timestamp": "2026-10-01T15:00:00", "total_usd": 105},
        {"timestamp": "2026-10-02T09:00:00", "total_usd": 0},  # skipped
        {"timestamp": "2026-10-02T10:00:00", "total_usd": 99},
    ]
    series, _ = flow_adjusted_series(points, [])
    rows = daily_equity_rows(series)

    assert [r["time"] for r in rows] == ["2026-10-01", "2026-10-02"]
    day1 = rows[0]
    assert (day1["open"], day1["high"], day1["low"], day1["close"]) == (100, 110, 100, 105)
    assert day1["twr_open"] == 100.0
    assert day1["twr_index"] == 105.0
    assert rows[1]["twr_index"] == 99.0
