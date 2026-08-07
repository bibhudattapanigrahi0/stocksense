"""Backtest engine correctness — deterministic synthetic price paths."""

import numpy as np
import pandas as pd

from pipeline_core.backtest.engine import build_daily_positions, compute_metrics, simulate
from pipeline_core.backtest.strategies import StrategyDef


def make_df(closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    closes = np.array(closes, dtype=float)
    opens = np.array(opens, dtype=float) if opens is not None else closes.copy()
    return pd.DataFrame(
        {
            "Open": opens,
            "High": np.maximum(opens, closes) * 1.01,
            "Low": np.minimum(opens, closes) * 0.99,
            "Close": closes,
            "Volume": np.full(len(closes), 100_000.0),
        },
        index=idx,
    )


def always_enter(df):
    s = pd.Series(False, index=df.index)
    s.iloc[0] = True
    return s


def never_exit(df):
    return pd.Series(False, index=df.index)


STRAT_HOLD = StrategyDef(
    id="t-hold", name="t", category="t", description="t",
    entry=always_enter, exit=never_exit, min_bars=0,
)
STRAT_TIME = StrategyDef(
    id="t-time", name="t", category="t", description="t",
    entry=always_enter, exit=never_exit, max_holding_days=3, min_bars=0,
)
STRAT_TARGET = StrategyDef(
    id="t-target", name="t", category="t", description="t",
    entry=always_enter, exit=never_exit, target_pct=0.10, min_bars=0,
)
STRAT_STOP = StrategyDef(
    id="t-stop", name="t", category="t", description="t",
    entry=always_enter, exit=never_exit, stop_loss_pct=0.05, min_bars=0,
)
STRAT_TRAIL = StrategyDef(
    id="t-trail", name="t", category="t", description="t",
    entry=always_enter, exit=never_exit, trailing_stop_pct=0.10, min_bars=0,
)


def test_entry_at_next_open_and_time_stop():
    # Signal on day 0 (close 100) → enter day 1 open (101). Time stop 3 days:
    # third held close is day 4 → exit day 5 open (105). Cost 25 bps round trip.
    df = make_df(closes=[100, 101, 102, 103, 104, 105], opens=[100, 101, 102, 103, 104, 105])
    res = simulate("T", df, STRAT_TIME, cost_roundtrip_bps=25)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_price == 101.0
    assert t.exit_price == 105.0
    assert t.exit_reason == "time"
    assert t.holding_days == 4
    expected = (105 / 101 - 1 - 0.0025) * 100
    assert abs(t.ret_pct - expected) < 0.01


def test_open_position_at_end_of_data():
    df = make_df(closes=[100, 101, 102, 103])
    res = simulate("T", df, STRAT_HOLD, cost_roundtrip_bps=25)
    assert res.trades == []
    assert res.open_trade is not None
    assert res.open_trade.is_open
    assert res.open_trade.exit_reason == "open"
    # Marked at last close: 103/101 - 1 - half cost
    assert abs(res.open_trade.ret_pct - ((103 / 101 - 1 - 0.00125) * 100)) < 0.01


def test_target_exit():
    df = make_df(closes=[100, 100, 105, 111, 112, 113])
    res = simulate("T", df, STRAT_TARGET, cost_roundtrip_bps=0)
    assert res.trades[0].exit_reason == "target"
    # Entry 100 (day1 open), target +10% hit on day 3 close (111) → exit day 4 open (112)
    assert res.trades[0].exit_price == 112.0


def test_stop_loss_exit():
    df = make_df(closes=[100, 100, 96, 94, 97, 99])
    res = simulate("T", df, STRAT_STOP, cost_roundtrip_bps=0)
    assert res.trades[0].exit_reason == "stop"
    # Stop -5% from entry 100 → triggered at close 94 (day 3) → exit day 4 open (97)
    assert res.trades[0].exit_price == 97.0


def test_trailing_stop_uses_peak():
    # Peak close 110, trailing 10% → trigger when close ≤ 99
    df = make_df(closes=[100, 100, 110, 105, 98, 102])
    res = simulate("T", df, STRAT_TRAIL, cost_roundtrip_bps=0)
    t = res.trades[0]
    assert t.exit_reason == "trailing"
    assert t.exit_price == 102.0  # triggered day 4 (close 98 ≤ 99) → exit day 5 open


def test_signal_exit():
    def exit_on_day2(df):
        s = pd.Series(False, index=df.index)
        s.iloc[2] = True
        return s

    strat = StrategyDef(
        id="t-sig", name="t", category="t", description="t",
        entry=always_enter, exit=exit_on_day2, min_bars=0,
    )
    df = make_df(closes=[100, 101, 102, 103])
    res = simulate("T", df, strat, cost_roundtrip_bps=0)
    assert res.trades[0].exit_reason == "signal"
    assert res.trades[0].exit_price == 103.0  # exit signal day 2 → exit day 3 open


def test_min_bars_warmup_blocks_early_entries():
    df = make_df(closes=list(range(100, 120)))
    strat = StrategyDef(
        id="t-warm", name="t", category="t", description="t",
        entry=always_enter, exit=never_exit, min_bars=50,  # > len(df) → no trades
    )
    res = simulate("T", df, strat)
    assert res.trades == []
    assert res.open_trade is None


def test_metrics_math():
    trades = [
        _trade(10.0), _trade(5.0), _trade(-4.0), _trade(-2.0),
    ]
    daily = pd.Series(
        [0.01, -0.005, 0.002, 0.0, 0.003],
        index=pd.date_range("2024-01-01", periods=5, freq="B"),
    )
    m = compute_metrics(trades, daily, benchmark_cagr=0.10, years=1.0)
    assert m["trades"] == 4
    assert m["win_rate"] == 50.0
    assert m["avg_win_pct"] == 7.5
    assert m["avg_loss_pct"] == -3.0
    assert m["profit_factor"] == 2.5
    assert m["expectancy_pct"] == 2.25
    assert m["benchmark_cagr_pct"] == 10.0
    assert "sharpe" in m and "max_drawdown_pct" in m


def test_daily_positions_equal_weight_mean():
    df_a = make_df(closes=[100, 100, 110, 110])  # +10% then flat
    df_b = make_df(closes=[100, 100, 90, 90])    # -10% then flat
    strat = StrategyDef(
        id="t-dp", name="t", category="t", description="t",
        entry=always_enter, exit=never_exit, max_holding_days=3, min_bars=0,
    )
    res_a = simulate("A", df_a, strat, 0)
    res_b = simulate("B", df_b, strat, 0)
    series = build_daily_positions({"A": res_a, "B": res_b}, {"A": df_a, "B": df_b})
    # Day of the ±10% moves: mean = 0
    assert not series.empty
    assert abs(series.max()) <= 0.10 + 1e-9


# ── "Normal drawdown" band (winners' max adverse excursion) ─────────

def test_mae_mfe_tracked_on_winning_trade():
    # Enter at 100, dip to 92 (−8% MAE), rally to 111 (+11% MFE) → target hit. The worst dip a
    # winner sat through is captured — the raw material for the hold-through-it band.
    df = make_df(closes=[100, 100, 92, 105, 111, 112])
    res = simulate("T", df, STRAT_TARGET, cost_roundtrip_bps=0)
    t = res.trades[0]
    assert t.exit_reason == "target"
    assert t.mae_pct == -8.0
    assert t.mfe_pct == 11.0


def test_winners_only_drawdown_band():
    from pipeline_core.backtest.engine import Trade

    def wtrade(ret, mae):
        return Trade(ticker="T", entry_date="2024-01-01", exit_date="2024-01-05",
                     entry_price=100.0, exit_price=100 + ret, ret_pct=ret,
                     holding_days=4, exit_reason="target", mae_pct=mae)

    # Winners dipped 4/6/10% first; losers' deeper dips must NOT drag the band down.
    trades = [wtrade(8, -4.0), wtrade(6, -6.0), wtrade(12, -10.0),
              wtrade(-15, -20.0), wtrade(-8, -18.0)]
    m = compute_metrics(trades, pd.Series(dtype=float), benchmark_cagr=None, years=1.0)
    assert m["n_winners"] == 3
    assert m["typical_dip_pct"] == 6.0        # median(|−4, −6, −10|)
    assert m["deep_dip_pct"] == 8.4           # 80th-pct of (4, 6, 10)


def test_drawdown_band_none_without_winners():
    from pipeline_core.backtest.engine import Trade

    trades = [Trade(ticker="T", entry_date="2024-01-01", exit_date="2024-01-05",
                    entry_price=100.0, exit_price=95.0, ret_pct=-5.0, holding_days=4,
                    exit_reason="stop", mae_pct=-7.0)]
    m = compute_metrics(trades, pd.Series(dtype=float), benchmark_cagr=None, years=1.0)
    assert m["n_winners"] == 0
    assert m["typical_dip_pct"] is None and m["deep_dip_pct"] is None


def _trade(ret):
    from pipeline_core.backtest.engine import Trade

    return Trade(
        ticker="T", entry_date="2024-01-01", exit_date="2024-01-05",
        entry_price=100.0, exit_price=100 + ret, ret_pct=ret,
        holding_days=4, exit_reason="signal",
    )
