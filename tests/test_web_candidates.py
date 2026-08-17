"""Tests for dashboard signal-candidate generation (Buy Candidate list)."""

from src.web.app import DashboardState, _rsi_class


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


class TestConfiguredRSILadder:
    """Bands follow the symbol's own ladder: buy_1 + 5 and sell_1 - 5."""

    LEVELS = {"buy_1": 35.0, "buy_2": 30.0, "sell_1": 80.0, "sell_2": 85.0}

    def _state(self, rsi_values, held=()):
        state = DashboardState()
        state.rsi_values = dict(rsi_values)
        state.rsi_thresholds = {s: dict(self.LEVELS) for s in rsi_values}
        # One call — it replaces the whole position dict, not merges.
        state.replace_positions_from_records([
            {
                "symbol": symbol, "market": "nasdaq", "quantity": 1,
                "avg_price": 100, "current_price": 100, "pnl": 0,
                "pnl_pct": 0, "rsi": rsi_values[symbol],
            }
            for symbol in held
        ])
        return state

    def test_buy_band_follows_buy_stage_1_plus_5(self):
        # buy_1 = 35 → candidate up to 40, not the old hardcoded 35
        state = self._state({"AAPL": 39.0, "MSFT": 41.0})
        symbols = [c.symbol for c in _buy_candidates(state)]
        assert symbols == ["AAPL"]

    def test_deep_band_follows_buy_stage_1(self):
        state = self._state({"AAPL": 34.0})
        buys = _buy_candidates(state)
        assert buys[0].signal_type == "buy_candidate_2"
        assert buys[0].threshold == 30.0  # next rung, not a hardcoded 25

    def test_sell_band_follows_sell_stage_1_minus_5(self):
        # sell_1 = 80 → candidate from 75, so the old 65 no longer qualifies
        state = self._state({"AAPL": 76.0, "MSFT": 66.0}, held=("AAPL", "MSFT"))
        state.update_signal_candidates()
        sells = [c for c in state.signal_candidates if c.signal_type == "sell_candidate"]
        assert [c.symbol for c in sells] == ["AAPL"]
        assert sells[0].threshold == 80.0

    def test_colour_agrees_with_candidate_listing(self, monkeypatch):
        """A symbol is coloured iff it is listed — checked over every RSI.

        Held symbols only: a sell candidate additionally requires a position,
        so an unheld overbought symbol is coloured without being listed.
        """
        import src.web.app as web_app

        rsi_values = {f"S{r}": float(r) for r in range(1, 100)}
        state = self._state(rsi_values, held=tuple(rsi_values))
        monkeypatch.setattr(web_app, "dashboard_state", state)
        state.update_signal_candidates()

        listed = {c.symbol for c in state.signal_candidates
                  if c.signal_type != "stop_loss_alert"}
        coloured = {s for s, r in rsi_values.items()
                    if _rsi_class(s, r) in ("rsi-warning", "rsi-danger")}
        assert listed == coloured
        # Sanity: the bands are the configured ones, not the old constants.
        assert "S40" in listed and "S41" not in listed   # buy_1 + 5
        assert "S75" in listed and "S74" not in listed   # sell_1 - 5


class TestDashboardPositionRecords:
    def test_replace_positions_from_records_updates_position_and_price_state(self):
        state = DashboardState()

        state.replace_positions_from_records([
            {
                "symbol": "aapl",
                "market": "nasdaq",
                "quantity": 2,
                "avg_price": 100,
                "current_price": 110,
                "pnl": 20,
                "pnl_pct": 10,
                "rsi": 42.5,
                "buy_stage": 1,
                "sell_stage": 2,
                "max_buy_stages": 3,
                "max_sell_stages": 3,
                "stage_cooldown_days": 7,
                "last_buy_date": "2026-06-20T09:30:00",
                "last_sell_date": "2026-06-21T10:45:00",
                "stop_loss_pct": -8,
                "stop_loss_distance": 18,
            },
        ])

        assert state.positions["AAPL"].current_price == 110
        assert state.positions["AAPL"].market == "NASDAQ"
        assert state.positions["AAPL"].buy_stage_reset_remaining
        assert state.positions["AAPL"].sell_stage_reset_remaining
        assert state.rsi_values["AAPL"] == 42.5
        assert state.rsi_prices["AAPL"] == {"price": 110, "market": "NASDAQ"}
