"""Strategy robustness — walk-forward consistency + trade-sequence bootstrap.
Deterministic synthetic paths, network-free."""

from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline_core.backtest import robustness as rb
from pipeline_core.backtest.strategies import StrategyDef


def _df(closes) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=len(closes), freq="B")
    c = np.asarray(closes, float)
    return pd.DataFrame(
        {"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": np.full(len(c), 1e5)},
        index=idx,
    )


def _periodic_entry(df):
    s = pd.Series(False, index=df.index)
    s.iloc[::10] = True                      # a fresh entry every 10 bars → trades in every window
    return s


def _never_exit(df):
    return pd.Series(False, index=df.index)


STRAT = StrategyDef(id="t-robust", name="t", category="t", description="t",
                    entry=_periodic_entry, exit=_never_exit, target_pct=0.05, stop_loss_pct=0.05, min_bars=0)

# ~520 business days of gentle uptrend + a flat benchmark.
_FRAMES = {"AAA": _df(list(100 * (1.001 ** np.arange(520))))}
_BENCH = _df(list(100 * (1.0005 ** np.arange(520))))


# ── bootstrap_trades ────────────────────────────────────────────────


def test_bootstrap_needs_enough_trades():
    assert rb.bootstrap_trades([0.01, 0.02, -0.01], n_boot=100) is None      # < 5 trades


def test_bootstrap_all_wins_low_loss_prob():
    b = rb.bootstrap_trades([0.03] * 20, n_boot=500)
    assert b["n_trades"] == 20 and b["prob_loss"] == 0.0 and b["prob_ruin"] == 0.0
    assert b["total_return_pct"]["p50"] > 0


def test_bootstrap_all_losses_high_loss_prob():
    b = rb.bootstrap_trades([-0.03] * 20, n_boot=500)
    assert b["prob_loss"] == 1.0 and b["total_return_pct"]["p50"] < 0


def test_bootstrap_is_deterministic_with_seed():
    a = rb.bootstrap_trades([0.02, -0.01, 0.03, -0.02, 0.01] * 6, n_boot=400, seed=7)
    b = rb.bootstrap_trades([0.02, -0.01, 0.03, -0.02, 0.01] * 6, n_boot=400, seed=7)
    assert a == b
    assert 0.0 <= a["prob_loss"] <= 1.0 and 0.0 <= a["prob_ruin"] <= 1.0


# ── walk_forward ────────────────────────────────────────────────────


def test_walk_forward_splits_and_summarizes():
    wf = rb.walk_forward(STRAT, _FRAMES, _BENCH, windows=4)
    assert wf["n_windows"] == 4 and len(wf["windows"]) == 4
    assert wf["verdict"] in {"robust", "mixed", "fragile", "insufficient"}
    for w in wf["windows"]:
        assert {"start", "end", "trades", "cagr_pct", "sharpe", "total_return_pct"} <= set(w)
    # An uptrend with a 5% target should win in most windows.
    assert wf["pct_windows_positive"] is not None and wf["pct_windows_positive"] >= 50


def test_walk_forward_short_history_returns_none():
    short = {"AAA": _df(list(100 * (1.001 ** np.arange(60))))}   # < 180 days
    assert rb.walk_forward(STRAT, short, None, windows=4) is None


# ── full report ─────────────────────────────────────────────────────


def test_robustness_report_bundle():
    rep = rb.robustness_report(STRAT, _FRAMES, _BENCH, years=2.0, windows=3, n_boot=300)
    assert {"full_metrics", "n_closed_trades", "walk_forward", "bootstrap"} <= set(rep)
    assert rep["n_closed_trades"] >= 5
    assert rep["walk_forward"]["n_windows"] == 3
    assert rep["bootstrap"]["n_trades"] == rep["n_closed_trades"]
