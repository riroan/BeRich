"""Next-trigger plan shown on the dashboard positions and symbol chart."""

import pytest

from src.strategy.plan import position_plan

PARAMS = {
    "avg_down_levels": [[30, 0.5], [25, 0.3], [20, 0.2]],
    "sell_levels": [[65, 0.3], [70, 0.3], [75, 0.4]],
    "stop_loss_levels": [[-10, 0.5], [-20, 1.0]],
}


def test_plan_reads_the_next_rung_of_each_ladder():
    plan = position_plan(PARAMS, buy_stage=1, sell_stage=0, sl_stage=1, avg_price=100)

    assert plan["next_buy_rsi"] == 25 and plan["next_buy_stage"] == 2
    assert plan["next_sell_rsi"] == 65 and plan["next_sell_portion"] == 0.3
    assert plan["stop_pct"] == -20
    assert plan["stop_price"] == pytest.approx(80.0)


def test_spent_ladders_report_none():
    plan = position_plan(PARAMS, buy_stage=3, sell_stage=3, sl_stage=2, avg_price=100)

    assert plan["next_buy_rsi"] is None
    assert plan["next_sell_rsi"] is None
    assert plan["stop_price"] is None


def test_scalar_stop_loss_and_empty_ladder():
    assert position_plan({"stop_loss": -8}, 0, 0, 0, 50)["stop_price"] == pytest.approx(46.0)
    # An explicitly empty stop ladder means no stop is armed
    assert position_plan({"stop_loss_levels": []}, 0, 0, 0, 50)["stop_price"] is None
