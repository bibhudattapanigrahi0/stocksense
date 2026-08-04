"""
Strategy robustness — walk-forward + Monte-Carlo confidence for a single strategy.

A single 10-year equity curve is one draw from a noisy process. This adds two honesty checks that
the leaderboard's point metrics can't give you:

  • **walk-forward** — split the history into sequential out-of-sample windows and require the edge
    to show up in most of them, not just once (guards against a curve carried by one lucky regime);
  • **trade-sequence bootstrap** — resample the realized trades thousands of times to build
    distributions of total return and max-drawdown, with a probability of a losing outcome and a
    probability of ruin (a drawdown past a chosen threshold).

Pure computation over already-loaded OHLCV frames + the strategy def — it reuses
`engine.backtest_strategy` (which already supports `entry_start`/`entry_end` windows). The async
data loading lives in `runner.run_robustness`.

Caveat (documented, not hidden): the bootstrap treats trade returns as an exchangeable sequence
(sequence risk via resampling), a standard simplification — it characterizes the *return
distribution*, not the exact capital-constrained portfolio path.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from pipeline_core.backtest.engine import backtest_strategy
from pipeline_core.backtest.strategies import StrategyDef


def _naive(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize(None) if t.tz is not None else t


def walk_forward(strategy: StrategyDef, frames: dict, benchmark_df, *,
                 windows: int = 4, cost_bps: float = 25.0) -> Optional[dict]:
    """Run the strategy across `windows` sequential out-of-sample sub-periods and summarize how
    consistently it held up. Entries are restricted to each window; indicator warm-up still uses the
    full history (no look-ahead), exactly like the train/test split the engine already supports."""
    starts = [df.index.min() for df in frames.values() if df is not None and not df.empty]
    ends = [df.index.max() for df in frames.values() if df is not None and not df.empty]
    if not starts:
        return None
    start, end = _naive(min(starts)), _naive(max(ends))
    total_days = (end - start).days
    if total_days < 180:
        return None
    windows = max(2, min(windows, total_days // 90))         # need ~90d of entries per window
    edges = [start + pd.Timedelta(days=int(total_days * i / windows)) for i in range(windows + 1)]

    rows = []
    for i in range(windows):
        w0, w1 = edges[i], edges[i + 1]
        yrs = max((w1 - w0).days / 365.25, 0.1)
        bt = backtest_strategy(strategy, frames, benchmark_df, yrs, cost_bps, entry_start=w0, entry_end=w1)
        m = bt["metrics"]
        rows.append({
            "start": str(w0.date()), "end": str(w1.date()), "trades": m["trades"],
            "cagr_pct": m["cagr_pct"], "sharpe": m["sharpe"], "win_rate": m["win_rate"],
            "max_drawdown_pct": m["max_drawdown_pct"], "total_return_pct": m["total_return_pct"],
        })

    decided = [w for w in rows if w["trades"] > 0]
    positive = [w for w in decided if w["total_return_pct"] > 0]
    n_dec = len(decided)
    pct_pos = round(len(positive) / n_dec * 100, 1) if n_dec else None
    sharpes = [w["sharpe"] for w in decided]
    if pct_pos is None:
        verdict = "insufficient"
    elif pct_pos >= 75:
        verdict = "robust"
    elif pct_pos >= 50:
        verdict = "mixed"
    else:
        verdict = "fragile"
    return {
        "windows": rows, "n_windows": windows, "n_with_trades": n_dec,
        "pct_windows_positive": pct_pos,
        "median_sharpe": round(float(np.median(sharpes)), 2) if sharpes else None,
        "mean_cagr_pct": round(float(np.mean([w["cagr_pct"] for w in decided])), 2) if decided else None,
        "verdict": verdict,
    }


def bootstrap_trades(ret_fracs: list[float], *, n_boot: int = 2000, ruin_dd: float = 0.5,
                     seed: int = 42) -> Optional[dict]:
    """Resample the closed-trade return sequence (with replacement) `n_boot` times and summarize the
    resulting compounded-equity distribution: percentile total return and max drawdown, the
    probability of a losing run, and the probability of ruin (drawdown past `ruin_dd`)."""
    r = np.asarray([x for x in ret_fracs if x is not None and np.isfinite(x)], dtype=float)
    n = len(r)
    if n < 5:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    samp = r[idx]                                            # (n_boot, n) resampled trade returns
    eq = np.cumprod(1.0 + samp, axis=1)
    total = eq[:, -1] - 1.0
    peak = np.maximum.accumulate(eq, axis=1)
    max_dd = (eq / peak - 1.0).min(axis=1)

    def band(a):
        return {"p5": round(float(np.percentile(a, 5)) * 100, 1),
                "p50": round(float(np.percentile(a, 50)) * 100, 1),
                "p95": round(float(np.percentile(a, 95)) * 100, 1)}

    return {
        "n_trades": n, "n_boot": n_boot,
        "total_return_pct": band(total),
        "max_drawdown_pct": band(max_dd),
        "prob_loss": round(float(np.mean(total < 0)), 3),
        "prob_ruin": round(float(np.mean(max_dd <= -abs(ruin_dd))), 3),
        "ruin_dd_pct": round(abs(ruin_dd) * 100, 1),
    }


def robustness_report(strategy: StrategyDef, frames: dict, benchmark_df, years: float, *,
                      cost_bps: float = 25.0, windows: int = 4, n_boot: int = 2000) -> dict:
    """Full robustness bundle for one strategy: headline metrics, walk-forward consistency, and the
    trade bootstrap."""
    full = backtest_strategy(strategy, frames, benchmark_df, years, cost_bps)
    closed = [t.ret_pct / 100.0 for t in full["trades"] if not t.is_open]
    return {
        "full_metrics": full["metrics"],
        "n_closed_trades": len(closed),
        "walk_forward": walk_forward(strategy, frames, benchmark_df, windows=windows, cost_bps=cost_bps),
        "bootstrap": bootstrap_trades(closed, n_boot=n_boot),
    }
