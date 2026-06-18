"""Tests for dashboard signal-candidate generation (Buy Candidate list)."""

from src.web.app import DashboardState


def _buy_candidates(state):
    state.update_signal_candidates()
    return [c for c in state.signal_candidates if "buy" in c.signal_type]


class TestBuyCandidateDedup:
    """RSI <= 30 must produce exactly ONE buy candidate row, not two.

    Regression: the <=35 and <=30 bands overlapped, so an oversold symbol
    was appended as both buy_candidate and buy_candidate_2 and showed up
    twice in the Buy Candidate list.
    """

    def test_deep_oversold_appears_once(self):
        state = DashboardState()
        state.rsi_values = {"AAPL": 28.0}

        buys = _buy_candidates(state)

        assert len(buys) == 1
        assert buys[0].symbol == "AAPL"
        assert buys[0].signal_type == "buy_candidate_2"

    def test_rsi_exactly_30_appears_once(self):
        state = DashboardState()
        state.rsi_values = {"AAPL": 30.0}

        buys = _buy_candidates(state)

        assert len(buys) == 1

    def test_approaching_band_still_works(self):
        state = DashboardState()
        state.rsi_values = {"MSFT": 33.0}  # 30 < rsi <= 35

        buys = _buy_candidates(state)

        assert len(buys) == 1
        assert buys[0].signal_type == "buy_candidate"

    def test_no_symbol_duplicated_across_band(self):
        state = DashboardState()
        state.rsi_values = {"AAPL": 28.0, "MSFT": 33.0, "QQQ": 55.0}

        buys = _buy_candidates(state)
        symbols = [c.symbol for c in buys]

        assert len(symbols) == len(set(symbols))  # no duplicates
        assert "QQQ" not in symbols  # not oversold → not a buy candidate
