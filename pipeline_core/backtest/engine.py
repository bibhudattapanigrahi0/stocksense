"""
Backtest engine — trade simulation, portfolio aggregation, and metrics.

Execution model (long-only, no lookahead):
  - Signal on day T → enter at day T+1 OPEN.
  - Exit conditions checked on each close while in position → exit at next
    OPEN. Priority on the same day: stop-loss > trailing > target > time > signal.
  - End of data with an open position → mark exit at the final close
    (recorded separately, and reported as the current open position).
  - Costs: round-trip cost in basis points (default 25 bps ≈ Indian delivery
    round trip with STT + charges + slippage).

Portfolio aggregation (documented approximation):
  daily portfolio return = mean daily return across all active positions
  (equal weight, unlimited fragmentation, flat when nothing is active).
  This measures *signal quality*, not a capital-constrained portfolio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from pipeline_core.backtest.strategies import StrategyDef
from pipeline_core.utils import round_price


@dataclass
class Trade:
    ticker: str
    entry_date: str
    exit_date: Optional[str]
    entry_price: float
    exit_price: Optional[float]
    ret_pct: float          # net of costs (unrealized mark if open)
    holding_days: int
    exit_reason: str        # signal | stop | trailing | target | time | end_of_data | open
    is_open: bool = False
    mae_pct: float = 0.0    # max adverse excursion: worst intrabar drawdown from entry (≤0, gross of cost)
    mfe_pct: float = 0.0    # max favorable excursion: best intrabar gain from entry (≥0, gross of cost)


@dataclass
class SimResult:
    trades: list[Trade] = field(default_factory=list)
    open_trade: Optional[Trade] = None
    daily_positions: Optional[pd.DataFrame] = None  # date → active count & daily ret


def simulate(
    ticker: str,
    df: pd.DataFrame,
    strategy: StrategyDef,
    cost_roundtrip_bps: float = 25.0,
    entry_start=None,
    entry_end=None,
) -> SimResult:
    """
    Simulate one strategy on one stock's OHLCV history.
    Returns closed trades plus any still-open position at the last bar.

    entry_start / entry_end (dates, optional): restrict trade *entries* to
    [entry_start, entry_end). Used for train/test (in-sample vs out-of-sample)
    splits — warm-up still uses the full history, so indicators are correct and
    there is no look-ahead. Defaults None = enter on any warmed-up signal.
    """
    if df is None or len(df) < strategy.min_bars + 2:
        return SimResult()

    entry_sig = strategy.entry(df).reindex(df.index).fillna(False).astype(bool).to_numpy()
    exit_sig = strategy.exit(df).reindex(df.index).fillna(False).astype(bool).to_numpy()

    # Signals only count once the strategy has enough warm-up history
    warmup = np.zeros(len(df), dtype=bool)
    warmup[strategy.min_bars:] = True
    entry_sig = entry_sig & warmup

    # Restrict entries to a date window (train/test split) — independent of warm-up.
    if entry_start is not None or entry_end is not None:
        idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
        if entry_start is not None:
            entry_sig = entry_sig & np.asarray(idx >= pd.Timestamp(entry_start))
        if entry_end is not None:
            entry_sig = entry_sig & np.asarray(idx < pd.Timestamp(entry_end))

    opens = df["Open"].to_numpy(dtype=float)
    closes = df["Close"].to_numpy(dtype=float)
    dates = df.index
    n = len(df)
    cost = cost_roundtrip_bps / 10_000.0

    result = SimResult()
    i = 0
    while i < n - 1:
        if not entry_sig[i]:
            i += 1
            continue

        entry_i = i + 1
        entry_px = opens[entry_i]
        if not np.isfinite(entry_px) or entry_px <= 0:
            i += 1
            continue

        peak = entry_px
        worst = 0.0        # max adverse excursion (most-negative close-vs-entry seen while held)
        best = 0.0         # max favorable excursion (best close-vs-entry seen while held)
        exit_i: Optional[int] = None
        reason = "signal"

        for j in range(entry_i, n):
            c = closes[j]
            if not np.isfinite(c):
                continue
            peak = max(peak, c)
            ret = c / entry_px - 1.0
            worst = min(worst, ret)
            best = max(best, ret)
            held = j - entry_i

            if strategy.stop_loss_pct and c <= entry_px * (1 - strategy.stop_loss_pct):
                exit_i, reason = j + 1, "stop"
            elif strategy.trailing_stop_pct and c <= peak * (1 - strategy.trailing_stop_pct):
                exit_i, reason = j + 1, "trailing"
            elif strategy.target_pct and ret >= strategy.target_pct:
                exit_i, reason = j + 1, "target"
            elif strategy.max_holding_days and held >= strategy.max_holding_days:
                exit_i, reason = j + 1, "time"
            elif exit_sig[j]:
                exit_i, reason = j + 1, "signal"

            if exit_i is not None:
                if exit_i >= n:  # no next bar → exit at this close
                    exit_i, reason = j, "end_of_data"
                break

        if exit_i is None:
            # Still in position at the last bar → open trade, marked to market
            last_close = closes[-1]
            ret = last_close / entry_px - 1.0 - cost / 2 if np.isfinite(last_close) else 0.0
            result.open_trade = Trade(
                ticker=ticker,
                entry_date=str(dates[entry_i].date()),
                exit_date=None,
                entry_price=round_price(entry_px),
                exit_price=None,
                ret_pct=round(ret * 100, 2),
                holding_days=n - 1 - entry_i,
                exit_reason="open",
                is_open=True,
                mae_pct=round(worst * 100, 2),
                mfe_pct=round(best * 100, 2),
            )
            break

        exit_px = opens[exit_i] if reason != "end_of_data" else closes[exit_i]
        if not np.isfinite(exit_px) or exit_px <= 0:
            exit_px = closes[exit_i]
        gross = exit_px / entry_px - 1.0
        net = gross - cost

        result.trades.append(
            Trade(
                ticker=ticker,
                entry_date=str(dates[entry_i].date()),
                exit_date=str(dates[exit_i].date()),
                entry_price=round_price(entry_px),
                exit_price=round_price(exit_px),
                ret_pct=round(net * 100, 2),
                holding_days=exit_i - entry_i,
                exit_reason=reason,
                mae_pct=round(worst * 100, 2),
                mfe_pct=round(best * 100, 2),
            )
        )
        i = exit_i  # next signal search resumes after this exit

    return result


def build_daily_positions(all_results: dict[str, SimResult], df_by_ticker: dict[str, pd.DataFrame]) -> pd.Series:
    """
    Daily portfolio return series: mean of active positions' daily returns
    per day (0 on days with no active position). Close-to-close marks;
    entries/exports hit at open are approximated to close — documented.
    """
    daily_rets: dict[pd.Timestamp, list[float]] = {}
    for ticker, res in all_results.items():
        df = df_by_ticker.get(ticker)
        if df is None or df.empty:
            continue
        if df.index.tz is not None:  # defensive: align with naive trade dates
            df = df.copy()
            df.index = df.index.tz_localize(None)
        close_ret = df["Close"].pct_change()
        for trade in [*res.trades, *([res.open_trade] if res.open_trade else [])]:
            start = pd.Timestamp(trade.entry_date)
            end = pd.Timestamp(trade.exit_date) if trade.exit_date else df.index[-1]
            window = close_ret.loc[start:end].dropna()
            for dt, r in window.items():
                daily_rets.setdefault(dt, []).append(float(r))

    if not daily_rets:
        return pd.Series(dtype=float)

    series = pd.Series(
        {dt: float(np.mean(rets)) for dt, rets in daily_rets.items()},
        name="portfolio_daily_ret",
    ).sort_index()
    return series


def compute_metrics(
    trades: list[Trade],
    daily_ret: pd.Series,
    benchmark_cagr: Optional[float],
    years: float,
) -> dict:
    """Leaderboard metrics for one strategy."""
    closed = [t for t in trades if not t.is_open]
    n = len(closed)
    wins = [t for t in closed if t.ret_pct > 0]
    losses = [t for t in closed if t.ret_pct <= 0]

    win_rate = len(wins) / n * 100 if n else 0.0
    avg_win = float(np.mean([t.ret_pct for t in wins])) if wins else 0.0

    # "Normal drawdown" band — how far a WINNING trade typically dipped before it worked out (median
    # |MAE|), plus a wider 80th-percentile "beyond normal" marker. Surfaced on the idea board as
    # hold-through guidance ("this setup usually dips ~X% first — that's normal"), NOT a stop. Winners
    # only, since it answers "dips that resolved into wins". None when too few winners to be meaningful.
    win_maes = [abs(t.mae_pct) for t in wins if t.mae_pct is not None]
    if win_maes:
        typical_dip = round(float(np.median(win_maes)), 1)
        deep_dip = round(float(np.percentile(win_maes, 80)), 1)
    else:
        typical_dip = deep_dip = None
    avg_loss = float(np.mean([t.ret_pct for t in losses])) if losses else 0.0
    gross_win = sum(t.ret_pct for t in wins)
    gross_loss = abs(sum(t.ret_pct for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    expectancy = float(np.mean([t.ret_pct for t in closed])) if n else 0.0
    avg_hold = float(np.mean([t.holding_days for t in closed])) if n else 0.0

    # Equity curve metrics
    if not daily_ret.empty and years > 0:
        equity = (1 + daily_ret).cumprod()
        total_ret = float(equity.iloc[-1] - 1)
        cagr = float(equity.iloc[-1] ** (1 / years) - 1) if equity.iloc[-1] > 0 else -1.0
        running_max = equity.cummax()
        drawdown = equity / running_max - 1
        max_dd = float(drawdown.min())
        vol = float(daily_ret.std() * math.sqrt(252))
        sharpe = float(daily_ret.mean() / daily_ret.std() * math.sqrt(252)) if daily_ret.std() > 0 else 0.0
        downside = daily_ret[daily_ret < 0]
        sortino = (
            float(daily_ret.mean() / downside.std() * math.sqrt(252))
            if len(downside) > 1 and downside.std() > 0 else 0.0
        )
    else:
        equity = pd.Series(dtype=float)
        total_ret = cagr = max_dd = vol = sharpe = sortino = 0.0

    calmar = (cagr / abs(max_dd)) if max_dd < 0 else 0.0

    return {
        "trades": n,
        "win_rate": round(win_rate, 1),
        "n_winners": len(wins),
        "typical_dip_pct": typical_dip,     # median worst-dip of winning trades (the "normal drawdown")
        "deep_dip_pct": deep_dip,           # 80th-pct worst-dip of winners ("beyond this = unusual")
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if math.isfinite(profit_factor) else "inf",
        "expectancy_pct": round(expectancy, 2),
        "avg_holding_days": round(avg_hold, 1),
        "total_return_pct": round(total_ret * 100, 1),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "ann_volatility_pct": round(vol * 100, 1),
        "benchmark_cagr_pct": round(benchmark_cagr * 100, 2) if benchmark_cagr is not None else None,
        "alpha_vs_benchmark_pct": (
            round((cagr - benchmark_cagr) * 100, 2) if benchmark_cagr is not None else None
        ),
        "_equity": equity,
    }


def backtest_strategy(
    strategy: StrategyDef,
    df_by_ticker: dict[str, pd.DataFrame],
    benchmark_df: Optional[pd.DataFrame],
    years: float,
    cost_roundtrip_bps: float = 25.0,
    entry_start=None,
    entry_end=None,
) -> dict:
    """Run one strategy across the whole universe and aggregate.

    entry_start / entry_end restrict trade entries to a date window (train/test
    split); see simulate(). years should reflect the entered window's span so
    annualized metrics (CAGR, etc.) are correct."""
    results: dict[str, SimResult] = {}
    for ticker, df in df_by_ticker.items():
        try:
            res = simulate(ticker, df, strategy, cost_roundtrip_bps, entry_start, entry_end)
            if res.trades or res.open_trade:
                results[ticker] = res
        except Exception:
            continue  # one bad frame must not kill the run

    all_trades = [t for res in results.values() for t in res.trades]
    open_trades = [res.open_trade for res in results.values() if res.open_trade]

    daily_ret = build_daily_positions(results, df_by_ticker)

    benchmark_cagr = None
    benchmark_equity = pd.Series(dtype=float)
    if benchmark_df is not None and not benchmark_df.empty:
        b_close = benchmark_df["Close"].dropna()
        benchmark_equity = b_close / b_close.iloc[0]
        benchmark_cagr = float(benchmark_equity.iloc[-1] ** (1 / years) - 1) if years > 0 else None

    metrics = compute_metrics(all_trades, daily_ret, benchmark_cagr, years)
    equity = metrics.pop("_equity")

    return {
        "strategy_id": strategy.id,
        "metrics": metrics,
        "equity": equity,
        "benchmark_equity": benchmark_equity,
        "trades": all_trades,
        "open_trades": open_trades,
        "stocks_traded": len(results),
    }
