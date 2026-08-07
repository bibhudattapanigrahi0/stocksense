"""
Backtest of the curated-basket **construction method** — a held-basket, weekly-rebalanced historical
simulation with anti-lookahead. Answers the question the live basket can't: *would this method have
made money?*

Each rebalance date the board is **reconstructed as of that date** (point-in-time), the same
`basket.build_basket` picks a diversified, regime-scaled, hysteresis-sticky basket, and its weights are
held until the next rebalance. Daily portfolio returns compound into an equity curve; turnover is
charged at each rebalance. Metrics reuse the engine's `compute_metrics` (CAGR / Sharpe / maxDD / alpha
vs benchmark).

**Anti-lookahead is the whole point.** Strategy `entry(df)` is causal (its indicators are trailing), so
precomputing each signal over full history and reading it as of date D is identical to computing it on
`df.loc[:D]` — no future leak. Regime (benchmark vs its 200-DMA) and correlation clusters are likewise
computed only from data ≤ D; forward returns come from D+1 onward.

**Scope boundary (deliberate):** this validates the *construction* method — conviction aggregation +
cross-horizon diversification + regime-scaled ¼-Kelly + hysteresis. The live board's full-sample
calibration / fusion win-rate layer is intentionally NOT used (it would leak future outcomes into a
past date), so `build_basket` here ranks on conviction × regime × diversification and sizes by its
conviction fallback. An expanding-window per-strategy win rate is a possible follow-on.

`simulate_basket` is the pure, synthetic-data-testable core; `run_basket_backtest` adds the network
load + a background-job state, mirroring `bots/backtest.py`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from pipeline_core.backtest import basket
from pipeline_core.backtest.engine import compute_metrics
from pipeline_core.backtest.runner import (
    HORIZON_META, _idea_buy_range, _idea_stop, _idea_target, strategy_horizon,
)
from pipeline_core.backtest.strategies import StrategyDef
from pipeline_core.config import DATA_DIR, settings
from pipeline_core import dedup
from pipeline_core.utils import deep_sanitize, round_price

logger = logging.getLogger(__name__)

RESULTS_DIR = DATA_DIR / "backtests"
WARMUP_BARS = 210            # bars before the first rebalance (200-DMA + a little slack)
SIGNAL_LOOKBACK = 3          # a strategy "flags" a name if its entry fired within this many bars ≤ D
_REGIME_SCALE = 8.0          # benchmark % above/below its 200-DMA that maps to a full ±1 tilt
_HORIZONS = ("short", "medium", "long")
_MAX_CURVE_POINTS = 400      # downsample the persisted equity curve for a light payload


def _result_file(market: str = "IN"):
    return RESULTS_DIR / f"basket_backtest_{(market or 'IN').upper()}.json"


# ── Precompute: causal entry signals → a per-(strategy,ticker) "fired within lookback" array ──

def _rolling_fired(entry: pd.Series, calendar: pd.DatetimeIndex, lookback: int) -> np.ndarray:
    """A calendar-aligned bool array: True at each date iff the strategy's entry fired within the last
    `lookback` bars (inclusive). Causal — reindexing a trailing signal onto the calendar and rolling a
    max backward introduces no future information."""
    aligned = entry.reindex(calendar).fillna(False).astype(bool)
    return aligned.rolling(lookback, min_periods=1).max().fillna(0).to_numpy().astype(bool)


# ── Reconstruct the board as of one date, then build its basket ──

def _regime_asof(bench_close: np.ndarray, i: int) -> dict:
    """A point-in-time risk-on/off reading from the benchmark vs its 200-DMA at calendar index `i`.
    Returns the minimal {state, tilt} that `basket.build_basket` consumes for size scaling."""
    lo = max(0, i - 199)
    sma = float(np.mean(bench_close[lo:i + 1]))
    px = float(bench_close[i])
    tilt = 0.0 if sma <= 0 else max(-1.0, min(1.0, (px / sma - 1.0) * _REGIME_SCALE))
    state = "risk-on" if tilt > 0.1 else "risk-off" if tilt < -0.1 else "neutral"
    return {"state": state, "tilt": round(tilt, 3)}


def _reconstruct_board(frames: dict, closes: dict, fired: dict, strategies: list,
                       i: int, date, regime: dict, market: str) -> dict:
    """Build a horizon-board dict *as of* calendar index `i` from the precomputed signals — the
    point-in-time analog of `runner.horizon_ideas`, minus the full-sample metrics (which would leak).
    Conviction = how many strategies flagged the name in that horizon; plan geometry reuses the live
    `_idea_stop`/`_idea_target`/`_idea_buy_range`."""
    pools: dict[str, dict[str, dict]] = {h: {} for h in _HORIZONS}
    for sdef in strategies:
        h = strategy_horizon(sdef)
        for tk in frames:
            arr = fired.get((sdef.id, tk))
            if arr is None or not arr[i]:
                continue
            price = closes[tk][i]
            if not np.isfinite(price) or price <= 0:
                continue
            agg = pools[h].setdefault(tk, {"ticker": tk, "price": float(price),
                                           "_stops": [], "_targets": [], "cats": []})
            for sp in (sdef.stop_loss_pct, sdef.trailing_stop_pct):
                if sp is not None:
                    agg["_stops"].append(sp)
            if sdef.target_pct is not None:
                agg["_targets"].append(sdef.target_pct)
            agg["cats"].append(sdef.category)

    buckets = {}
    for h in _HORIZONS:
        cands = []
        for agg in pools[h].values():
            entry = agg["price"]
            stop_pct, stop_price, _ = _idea_stop(h, agg["_stops"], entry, market)
            target_pct, target_price, _ = _idea_target(h, agg["_targets"], entry, market)
            buy_low, buy_high = _idea_buy_range(h, entry)
            conv = len(agg["cats"])
            cands.append({
                "ticker": agg["ticker"], "price": entry,
                "buy_low": buy_low, "buy_high": buy_high,
                "stop_pct": stop_pct, "stop_price": stop_price,
                "target_pct": target_pct, "target_price": target_price,
                "risk_reward": round(target_pct / stop_pct, 2) if stop_pct else None,
                "conviction": conv, "eligible_conviction": conv,
                "rank_score": float(conv),          # point-in-time base score (no leaked win rate)
                "strategies": [{"category": c} for c in agg["cats"]],
            })
        buckets[h] = {"horizon": h, "label": HORIZON_META[h]["label"], "candidates": cands}
    return {"market": market, "horizons": list(_HORIZONS), "buckets": buckets, "regime": regime}


def _asof_clusters(frames: dict, closes: dict, tickers: set, i: int, lookback: int = 100) -> dict:
    """Correlation clusters over ONLY data ≤ i, for the candidate tickers (cross-horizon de-dup)."""
    if not settings.dedup_daily_enabled or len(tickers) < 2:
        return {}
    sub = {}
    lo = max(0, i - lookback)
    for tk in tickers:
        c = closes[tk][lo:i + 1]
        if np.isfinite(c).sum() >= 5:
            sub[tk] = pd.DataFrame({"Close": c})
    return dedup.cluster_symbols(sub, enabled=True)


# ── The pure simulation core ──

def simulate_basket(
    frames: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame,
    strategies: list[StrategyDef],
    *,
    market: str = "IN",
    rebalance_days: Optional[int] = None,
    cost_bps: Optional[float] = None,
    warmup: int = WARMUP_BARS,
    lookback: int = SIGNAL_LOOKBACK,
    progress=None,
) -> dict:
    """Held-basket, periodically-rebalanced simulation over the benchmark's trading calendar. Pure —
    no I/O — so tests drive it with synthetic frames.

    Weights are held constant within each rebalance segment (constant-mix; uninvested cash earns 0);
    turnover cost is charged where target weights change. Returns metrics + an equity curve + turnover /
    size / hit-rate extras. Empty result if there isn't enough history to run.
    """
    rb = int(rebalance_days or settings.basket_rebalance_days)
    cost = float(cost_bps if cost_bps is not None else settings.basket_backtest_cost_bps) / 10000.0
    calendar = benchmark.index
    n = len(calendar)
    if n <= warmup + rb:
        return {"ran": False, "note": "not enough history for the backtest window."}

    # Align every ticker's Close + daily return to the master calendar, once.
    closes: dict[str, np.ndarray] = {}
    rets: dict[str, np.ndarray] = {}
    for tk, df in frames.items():
        if df is None or getattr(df, "empty", True) or "Close" not in df:
            continue
        c = df["Close"].astype(float)
        c.index = pd.DatetimeIndex(c.index).tz_localize(None) if c.index.tz is not None else c.index
        c = c.reindex(calendar).ffill()
        closes[tk] = c.to_numpy()
        rets[tk] = c.pct_change().fillna(0.0).to_numpy()
    frames = {tk: frames[tk] for tk in closes}          # keep only usable tickers
    if not frames:
        return {"ran": False, "note": "no usable price history."}

    bench_close = benchmark["Close"].astype(float).reindex(calendar).ffill().to_numpy()
    bench_ret = pd.Series(bench_close).pct_change().fillna(0.0).to_numpy()

    # Precompute causal "fired within lookback" arrays per (strategy, ticker).
    fired: dict[tuple, np.ndarray] = {}
    for sdef in strategies:
        for tk, df in frames.items():
            try:
                sig = sdef.entry(df).astype(bool)
                sig.index = pd.DatetimeIndex(sig.index).tz_localize(None) if sig.index.tz is not None else sig.index
                fired[(sdef.id, tk)] = _rolling_fired(sig, calendar, lookback)
            except Exception:
                continue

    rebalance_set = set(range(warmup, n - 1, rb))
    n_rebalances = len(rebalance_set)
    start = min(rebalance_set)
    port_ret = np.zeros(n)
    weights: dict[str, float] = {}                       # weights held coming INTO each day
    prev_basket = None
    turnovers, sizes, seg_returns = [], [], []
    seg_accum, n_done = 0.0, 0

    # Day-by-day: every day earns the market return on the weights held into it (constant-mix); a
    # rebalance is decided at the CLOSE of its day (info ≤ that day) and its weights apply from the
    # next day — so no trading day's return is dropped and there's no lookahead.
    for d in range(start, n):
        if weights:
            day = sum(w * rets[tk][d] for tk, w in weights.items() if tk in rets)
            port_ret[d] += day
            seg_accum += day
        if d in rebalance_set:
            if weights:                                  # close the finished holding period (hit rate)
                seg_returns.append(seg_accum)
            seg_accum = 0.0
            regime = _regime_asof(bench_close, d)
            board = _reconstruct_board(frames, closes, fired, strategies, d, calendar[d], regime, market)
            cand = {c["ticker"] for h in _HORIZONS for c in board["buckets"][h]["candidates"]}
            bk = basket.build_basket(board, _asof_clusters(frames, closes, cand, d), prev_basket)
            prev_basket = bk
            new_w = {it["ticker"]: (it.get("alloc_pct") or 0.0) / 100.0 for it in bk.get("items", [])}
            sizes.append(len(new_w))
            tno = sum(abs(new_w.get(tk, 0.0) - weights.get(tk, 0.0)) for tk in set(new_w) | set(weights))
            turnovers.append(tno)
            port_ret[d] -= tno * cost                    # turnover cost charged on the rebalance day
            weights = new_w
            n_done += 1
            if progress:
                progress(n_done, n_rebalances)
    if weights:                                          # the final open holding period
        seg_returns.append(seg_accum)

    daily = pd.Series(port_ret[start:], index=calendar[start:], name="basket_daily_ret")
    years = max(len(daily) / 252.0, 1e-9)
    bench_seg = bench_ret[start:]
    bench_equity = float(np.prod(1 + bench_seg))
    bench_cagr = bench_equity ** (1 / years) - 1 if bench_equity > 0 else None
    metrics = compute_metrics([], daily, bench_cagr, years)
    equity = metrics.pop("_equity")

    # Downsampled equity curve (portfolio + benchmark, both NAV=1 at start) for the chart.
    bench_nav = np.cumprod(1 + bench_seg)
    stride = max(1, len(equity) // _MAX_CURVE_POINTS)
    curve = [{"date": str(equity.index[k].date()),
              "nav": round(float(equity.iloc[k]), 4),
              "benchmark_nav": round(float(bench_nav[k]), 4)}
             for k in range(0, len(equity), stride)]

    hits = sum(1 for s in seg_returns if s > 0)
    return {
        "ran": True,
        "metrics": metrics,
        "extras": {
            "rebalances": n_rebalances,
            "avg_turnover_pct": round(float(np.mean(turnovers)) * 100, 1) if turnovers else 0.0,
            "avg_basket_size": round(float(np.mean(sizes)), 2) if sizes else 0.0,
            "hit_rate_pct": round(hits / len(seg_returns) * 100, 1) if seg_returns else None,
            "rebalance_days": rb, "cost_bps": round(cost * 10000, 1),
            "years": round(years, 2),
        },
        "equity_curve": curve,
        "as_of_start": str(calendar[start].date()),
        "as_of_end": str(calendar[-1].date()),
    }


# ── Background-job wrapper (network load + progress), mirroring bots/backtest.py ──

job_state: dict = {"running": False, "stage": "idle", "progress_pct": 0.0, "detail": "",
                   "market": None, "started_at": None, "finished_at": None, "error": None}


def _set_job(stage: str, pct: float, detail: str = "") -> None:
    job_state.update(stage=stage, progress_pct=round(pct, 1), detail=detail)


def load_result(market: str = "IN") -> Optional[dict]:
    p = _result_file(market)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Could not read basket backtest ({p.name}): {e}")
        return None


def _persist(payload: dict, market: str) -> None:
    dest = _result_file(market)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(deep_sanitize(payload)), encoding="utf-8")
    tmp.replace(dest)


def status() -> dict:
    return dict(job_state)


async def run_basket_backtest(market: str = "IN", years: Optional[int] = None,
                              rebalance_days: Optional[int] = None) -> dict:
    """Load the universe + benchmark and run the held-basket backtest, persisting the result.
    Single-flight via `job_state`. Returns the result payload (also saved to disk)."""
    import asyncio

    from pipeline_core.backtest.data_loader import load_benchmark, load_universe_history
    from pipeline_core.backtest.discovery import promoted_strategies
    from pipeline_core.backtest.strategies import STRATEGIES
    from pipeline_core.markets import get_market
    from pipeline_core.services.market_data import get_index_tickers

    if job_state["running"]:
        return {"ran": False, "error": "A basket backtest is already running.", "status": status()}

    mk = (market or "IN").upper()
    yrs = int(years or settings.basket_backtest_years)
    job_state.update(running=True, error=None, market=mk, finished_at=None,
                     started_at=datetime.now(timezone.utc).isoformat())
    _set_job("starting", 0.0, mk)
    try:
        m = get_market(mk)
        _set_job("loading universe", 3.0, m.default_universe)
        tickers = await asyncio.to_thread(get_index_tickers, m.default_universe, mk)
        frames, load_stats = await load_universe_history(
            tickers, years=yrs, market=mk,
            progress_callback=lambda d, t: _set_job("loading history", 3.0 + 42.0 * d / max(t, 1),
                                                    f"{d}/{t} tickers"))
        benchmark = await load_benchmark(m.benchmark, yrs, market=mk)
        if not frames or benchmark is None or benchmark.empty:
            raise RuntimeError("No price history loaded — data source unreachable?")
        strategies = list(STRATEGIES) + promoted_strategies(mk)

        _set_job("simulating", 50.0, f"{len(frames)} names · {len(strategies)} strategies")
        result = await asyncio.to_thread(
            simulate_basket, frames, benchmark, strategies, market=mk,
            rebalance_days=rebalance_days,
            progress=lambda r, t: _set_job("simulating", 50.0 + 48.0 * r / max(t, 1), f"rebalance {r}/{t}"))

        payload = {
            "market": mk, "years": yrs, "generated_at": datetime.now(timezone.utc).isoformat(),
            "benchmark_label": m.benchmark_label, "universe": m.default_universe,
            "names": len(frames), "strategies": len(strategies),
            "load_stats": load_stats, **result,
        }
        _persist(payload, mk)
        _set_job("done", 100.0, "")
        return payload
    except Exception as e:
        job_state["error"] = str(e)
        logger.exception("Basket backtest failed")
        return {"ran": False, "error": str(e)}
    finally:
        job_state.update(running=False, finished_at=datetime.now(timezone.utc).isoformat())
