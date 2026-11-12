"""
Backtest runner — orchestrates full-universe backtests of the strategy
library, computes current buy/sell signals per strategy, persists results,
and tracks job progress for the API/UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from pipeline_core.backtest import basket, idea_ranking
from pipeline_core.backtest.data_loader import load_benchmark, load_universe_history
from pipeline_core.backtest.discovery import get_discovered_strategy, promoted_strategies
from pipeline_core.backtest.engine import backtest_strategy, simulate
from pipeline_core.backtest.strategies import (
    ALL_STRATEGIES, STRATEGIES, StrategyDef, strategies_for_market,
)
from pipeline_core.config import DATA_DIR, settings
from pipeline_core import calibration, dedup, fusion, sizing
from pipeline_core.services import market_regime, market_sentiment, smart_money
from pipeline_core.services.market_data import (
    aget_price_history,
    get_index_tickers,
    get_price_history,
    get_quote,
    get_stock_info,
)
from pipeline_core.utils import deep_sanitize, round_price

logger = logging.getLogger(__name__)

RESULTS_DIR = DATA_DIR / "backtests"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
# Backtest results are stored per-market so Strategy Lab / Trade Ideas / systematic
# portfolios each reflect the selected market independently.
_LEGACY_LATEST = RESULTS_DIR / "latest.json"  # pre-multi-market run (treated as IN)


def _latest_file(market: str = "IN"):
    return RESULTS_DIR / f"latest_{(market or 'IN').upper()}.json"


def _bias_disclaimer(index: str) -> str:
    return (
        f"Backtest caveats: tested on CURRENT {index} constituents (survivorship "
        "bias — names that dropped out of the universe are absent); long-only; "
        "next-open execution; round-trip costs applied; portfolio curve = "
        "equal-weight mean of active positions (signal-quality measure, not a "
        "capital-constrained portfolio); past performance ≠ future results. "
        "Decision-support research, not investment advice."
    )

BIAS_DISCLAIMER = _bias_disclaimer("Nifty 500")  # back-compat default (IN)

# In-memory job state (single run at a time, across all markets)
job_state: dict = {
    "running": False,
    "market": None,
    "stage": None,
    "progress_pct": 0.0,
    "detail": "",
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def _set_job(stage: str, pct: float, detail: str = "") -> None:
    job_state.update(stage=stage, progress_pct=round(pct, 1), detail=detail)


# Separate, lightweight job state for the fast signals-only refresh (runs independently of the
# heavy full backtest above — it never recomputes metrics, only re-scans current buy signals).
signals_job_state: dict = {
    "running": False, "market": None, "stage": None, "progress_pct": 0.0,
    "detail": "", "started_at": None, "finished_at": None, "error": None,
}


# ── Signal scanning ────────────────────────────────────────────────


def _last_close(df) -> Optional[float]:
    """Latest close as a positive, finite price — or None if the last bar is degenerate
    (0 / NaN / inf). Newly-listed or delisted coins can carry a bad final bar; seeding a
    buy signal off it produces an idea with entry_price 0 that can NEVER stop/target out
    (`_evaluate_idea_path` treats a falsy entry as still-open), so it gets carried forever.
    Guarding here at the source keeps those broken $0.00 cards off the board entirely."""
    try:
        px = float(df["Close"].iloc[-1])
    except Exception:
        return None
    return round_price(px) if np.isfinite(px) and px > 0 else None


def scan_current_signals(
    strategy: StrategyDef,
    df_by_ticker: dict[str, pd.DataFrame],
    lookback_days: int = 3,
) -> dict:
    """
    Current signal state per strategy:
      - BUY:  entry edge within the last `lookback_days` bars (fresh signal)
      - SELL: position closed (by any exit) within the last `lookback_days` bars
      - HOLDING: open position right now (entry date + unrealized %)
    """
    buys, sells, holding = [], [], []

    for ticker, df in df_by_ticker.items():
        if df is None or len(df) < strategy.min_bars + 2:
            continue
        try:
            res = simulate(ticker, df, strategy)
        except Exception:
            continue

        last_date = df.index[-1]

        if res.open_trade:
            holding.append({
                "ticker": ticker,
                "entry_date": res.open_trade.entry_date,
                "entry_price": res.open_trade.entry_price,
                "unrealized_pct": res.open_trade.ret_pct,
                "days_held": res.open_trade.holding_days,
            })
            continue

        # Fresh BUY: entry signal fired in the lookback window and no open trade
        if res.trades:
            last_trade = res.trades[-1]
            try:
                exit_dt = pd.Timestamp(last_trade.exit_date)
                days_since_exit = (last_date - exit_dt).days
            except Exception:
                days_since_exit = 9999
            if days_since_exit <= lookback_days and last_trade.exit_reason not in ("end_of_data",):
                sells.append({
                    "ticker": ticker,
                    "exit_date": last_trade.exit_date,
                    "exit_reason": last_trade.exit_reason,
                    "ret_pct": last_trade.ret_pct,
                    "days_held": last_trade.holding_days,
                })
                continue

        try:
            entry_sig = strategy.entry(df).reindex(df.index).fillna(False).astype(bool)
            window = entry_sig.to_numpy()[-lookback_days:]
            if window.any():
                px = _last_close(df)                 # skip a degenerate last bar (0/NaN)
                if px is not None:
                    fire_idx = int(np.flatnonzero(window)[-1])
                    fire_date = str(entry_sig.index[-lookback_days + fire_idx].date())
                    buys.append({
                        "ticker": ticker,
                        "signal_date": fire_date,
                        "price": px,
                    })
        except Exception:
            continue

    buys.sort(key=lambda x: x["signal_date"], reverse=True)
    return {
        "buy": buys,
        "sell": sells,
        "holding": sorted(holding, key=lambda x: x["unrealized_pct"], reverse=True),
        "as_of": str(df_by_ticker and next(iter(df_by_ticker.values())).index[-1].date() or ""),
    }


def scan_fresh_buys(strategy: StrategyDef, df_by_ticker: dict[str, pd.DataFrame],
                    lookback_days: int = 3) -> list[dict]:
    """Just the fresh-BUY signals for a strategy — the buy-only, `simulate`-free half of
    `scan_current_signals`. Computing `strategy.entry(df)` is a vectorized indicator check, so
    this is *seconds* over the whole universe (no per-bar simulation). Powers the fast idea
    refresh; the Trade Ideas board only reads `signals.buy`, so this is all it needs."""
    buys = []
    for ticker, df in df_by_ticker.items():
        if df is None or len(df) < strategy.min_bars + 2:
            continue
        try:
            entry_sig = strategy.entry(df).reindex(df.index).fillna(False).astype(bool)
        except Exception:
            continue
        window = entry_sig.to_numpy()[-lookback_days:]
        if window.any():
            px = _last_close(df)                     # skip a degenerate last bar (0/NaN)
            if px is not None:
                fire_idx = int(np.flatnonzero(window)[-1])
                fire_date = str(entry_sig.index[-lookback_days + fire_idx].date())
                buys.append({
                    "ticker": ticker,
                    "signal_date": fire_date,
                    "price": px,
                })
    buys.sort(key=lambda x: x["signal_date"], reverse=True)
    return buys


# ── Full run ───────────────────────────────────────────────────────


async def run_full_backtest(
    index: Optional[str] = None,
    years: int = 10,
    cost_roundtrip_bps: Optional[float] = None,
    strategy_ids: Optional[list[str]] = None,
    market: str = "IN",
) -> dict:
    """
    Execute the complete backtest: load 10y history for the universe,
    backtest every strategy, scan current signals, persist results.
    The universe and benchmark are market-aware (S&P 500/^GSPC for US,
    metals/gold, crypto/BTC); strategies are pure-price so they run unchanged.

    cost_roundtrip_bps: None (default) ⇒ use the market's realistic cost (config.market_cost_bps —
    crypto ~35, US ~10, …); pass a number to override. Crypto shorts additionally pay a per-day
    funding/borrow drag (config.crypto_funding_bps_per_day), modelled in the engine.
    """
    if job_state["running"]:
        raise RuntimeError("A backtest job is already running")

    from pipeline_core.config import market_cost_bps
    from pipeline_core.markets import get_market

    m = get_market(market)
    index = index or m.default_universe
    # Realistic per-market cost unless the caller overrode it; crypto shorts also pay funding.
    cost_bps = market_cost_bps(m.key) if cost_roundtrip_bps is None else cost_roundtrip_bps
    funding_bps = settings.crypto_funding_bps_per_day if m.key == "CRYPTO" else 0.0

    job_state.update(running=True, market=m.key, started_at=datetime.now(timezone.utc).isoformat(),
                     finished_at=None, error=None)
    t0 = time.time()

    try:
        _set_job("Loading index constituents", 2.0)
        tickers = await asyncio.to_thread(get_index_tickers, index, m.key)

        def on_progress(done: int, total: int) -> None:
            _set_job(f"Loading {years}y history ({done}/{total})", 2.0 + done / max(total, 1) * 38.0)

        _set_job(f"Loading {years}y price history", 3.0, f"{len(tickers)} tickers")
        df_by_ticker, load_stats = await load_universe_history(
            tickers, years=years, progress_callback=on_progress, market=m.key
        )
        benchmark_df = await load_benchmark(m.benchmark, years, market=m.key)

        if not df_by_ticker:
            raise RuntimeError("No price history loaded — data source unreachable?")

        # Merge the market's smart-money series onto each frame so the follow-the-smart-money
        # strategies have data (US 13F today; COT/flows/deals per market as sources land).
        # No-op / inert on markets without a source. See services/smart_money.enrich_frames.
        smart_money.enrich_frames(m.key, df_by_ticker)

        # Crypto: inject the market-wide BTC regime (the benchmark) onto every coin frame so the
        # market-gated strategies can sit out crypto winters — crypto's dominant risk factor is BTC, and
        # gating each coin on its own MA (the old per-coin proxy) lets the book crash with the market. The
        # column is read by strategies._mkt_risk_on/_off; absent ⇒ they fall back to the per-coin proxy.
        if m.key == "CRYPTO" and benchmark_df is not None and not benchmark_df.empty:
            market_regime.enrich_frames_with_market(df_by_ticker, benchmark_df["Close"])

        # Curated strategies (market-scoped — crypto runs its own trend/momentum set) + any
        # user-promoted discovered ones (they run like built-ins).
        available = list(strategies_for_market(m.key)) + promoted_strategies(m.key)
        strategies = available if strategy_ids is None else [s for s in available if s.id in strategy_ids]
        results = []

        for idx, strategy in enumerate(strategies):
            pct = 40.0 + (idx / len(strategies)) * 45.0
            _set_job(f"Backtesting {strategy.name}", pct, f"{idx + 1}/{len(strategies)}")

            bt = await asyncio.to_thread(
                backtest_strategy, strategy, df_by_ticker, benchmark_df, years, cost_bps,
                None, None, funding_bps
            )
            results.append(bt)

        # ── Current signals (re-simulate per strategy for open-trade state) ──
        signals = {}
        for idx, strategy in enumerate(strategies):
            pct = 85.0 + (idx / len(strategies)) * 12.0
            _set_job(f"Scanning signals: {strategy.name}", pct)
            signals[strategy.id] = await asyncio.to_thread(
                scan_current_signals, strategy, df_by_ticker, 3
            )

        _set_job("Persisting results", 98.0)
        payload = _serialize(results, signals, index, years, load_stats, cost_bps,
                             time.time() - t0, market=m.key, benchmark_label=m.benchmark_label)
        payload["funding_bps_per_day"] = funding_bps    # disclosed so the board can note the short drag
        _persist(payload, m.key)

        # Auto-track the freshly generated ideas so their win/loss can be followed over time.
        try:
            snap = await snapshot_current_ideas(market=m.key)
            logger.info("Idea tracker: +%d new (total %d)", snap.get("added", 0), snap.get("total", 0))
        except Exception as e:
            logger.warning("Idea snapshot failed: %s", e)

        job_state.update(running=False, finished_at=datetime.now(timezone.utc).isoformat())
        _set_job("Completed", 100.0)
        return payload

    except Exception as e:
        logger.error(f"Backtest run failed: {e}", exc_info=True)
        job_state.update(running=False, error=str(e),
                         finished_at=datetime.now(timezone.utc).isoformat())
        _set_job("Failed", 100.0, str(e))
        raise


async def run_robustness(
    strategy_id: str,
    market: str = "IN",
    years: int = 10,
    windows: int = 4,
    n_boot: int = 2000,
    cost_roundtrip_bps: float = 25.0,
) -> dict:
    """Walk-forward + Monte-Carlo robustness for one strategy: load the market's history once, then
    run sequential out-of-sample windows and a trade-sequence bootstrap. Resolves curated,
    promoted, and staged-discovered strategies. Runs inline (a few seconds warm)."""
    from pipeline_core.backtest import robustness as rb
    from pipeline_core.markets import get_market

    m = get_market(market)
    available = list(strategies_for_market(m.key)) + promoted_strategies(m.key)
    strat = next((s for s in available if s.id == strategy_id), None) or get_discovered_strategy(strategy_id, m.key)
    if strat is None:
        return {"error": f"unknown strategy '{strategy_id}' for {m.key}"}

    tickers = await asyncio.to_thread(get_index_tickers, m.default_universe, m.key)
    df_by_ticker, _ = await load_universe_history(tickers, years=years, market=m.key)
    benchmark_df = await load_benchmark(m.benchmark, years, market=m.key)
    if not df_by_ticker:
        return {"error": "no price history loaded — data source unreachable?"}

    report = await asyncio.to_thread(
        rb.robustness_report, strat, df_by_ticker, benchmark_df, float(years),
        cost_bps=cost_roundtrip_bps, windows=windows, n_boot=n_boot,
    )
    report.update(strategy_id=strat.id, strategy_name=strat.name, market=m.key,
                  years=years, cost_bps=cost_roundtrip_bps)
    return report


# ── Fast signals-only refresh (keeps the idea board fresh between full backtests) ──

# Approximate cash-market session windows in UTC (weekdays). Ignores holidays/DST — good enough
# to keep the background refresh from spinning overnight; crypto trades 24/7.
_SESSION_UTC = {
    "IN": (3.75, 10.25),    # 09:15–15:45 IST
    "US": (13.5, 20.0),     # 09:30–16:00 ET (standard time)
    "METALS": (13.5, 20.0), # track US roughly
}


def _market_active(market: str) -> bool:
    """Roughly, is this market in session now? CRYPTO is always active."""
    mk = (market or "IN").upper()
    if mk == "CRYPTO":
        return True
    win = _SESSION_UTC.get(mk)
    if win is None:
        return True
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:                       # Sat/Sun
        return False
    hour = now.hour + now.minute / 60.0
    return win[0] <= hour <= win[1]


async def _load_recent_universe(index: str, market: str, years: int) -> dict:
    """Fresh, short daily history for the whole universe (15-min price cache — much lighter than
    the 10y backtest loader), bounded by ingestion_concurrency. Skips names with too little data."""
    tickers = await asyncio.to_thread(get_index_tickers, index, market)
    sem = asyncio.Semaphore(settings.ingestion_concurrency)

    async def _one(tk: str):
        async with sem:
            try:
                df = await aget_price_history(tk, f"{years}y", "1d", market)
                return tk, (df if df is not None and len(df) >= 60 else None)
            except Exception:
                return tk, None

    pairs = await asyncio.gather(*(_one(t) for t in tickers))
    return {t: df for t, df in pairs if df is not None}


async def refresh_current_signals(market: str = "IN", years: int = 2, lookback_days: int = 3) -> dict:
    """Fast idea generation: re-scan every strategy's CURRENT buy signals on fresh prices and
    fold them into the market's latest results — **without** recomputing the 10y metrics. Keeps
    the Trade Ideas board current in seconds instead of the full backtest's minutes, then
    auto-tracks any new ideas. Requires a prior full backtest (for the strategy metrics)."""
    from pipeline_core.markets import get_market

    latest = load_latest(market)
    if latest is None:
        return {"updated": False, "note": f"No {market} backtest yet — run a full backtest first."}
    if signals_job_state["running"] or job_state["running"]:
        return {"updated": False, "note": "A backtest/refresh is already running."}

    mk = (latest.get("market") or market or "IN").upper()
    index = latest.get("index") or get_market(mk).default_universe
    signals_job_state.update(running=True, market=mk, error=None,
                             started_at=datetime.now(timezone.utc).isoformat(), finished_at=None)
    t0 = time.time()
    try:
        df_by_ticker = await _load_recent_universe(index, mk, years)
        if not df_by_ticker:
            return {"updated": False, "note": "No fresh price history loaded."}

        by_id = {s.id: s for s in strategies_for_market(mk)}
        for s in promoted_strategies(mk):        # promoted discovered strategies refresh too
            by_id.setdefault(s.id, s)
        as_of = ""
        total_buys = 0
        for strat_entry in latest["strategies"]:
            sdef = by_id.get(strat_entry["id"])
            if sdef is None:
                continue
            buys = await asyncio.to_thread(scan_fresh_buys, sdef, df_by_ticker, lookback_days)
            sig = strat_entry.setdefault("signals", {"buy": [], "sell": [], "holding": []})
            sig["buy"] = buys                       # refresh buys; keep sell/holding from last full run
            total_buys += len(buys)
            if df_by_ticker:
                as_of = str(next(iter(df_by_ticker.values())).index[-1].date())
                sig["as_of"] = as_of

        now_iso = datetime.now(timezone.utc).isoformat()
        latest["signals_refreshed_at"] = now_iso
        latest["signals_as_of"] = as_of
        _persist(latest, mk)

        added = 0
        try:
            added = (await snapshot_current_ideas(market=mk)).get("added", 0)   # auto-track new ideas
        except Exception as e:
            logger.warning("Idea snapshot after signal refresh failed: %s", e)

        signals_job_state.update(running=False, finished_at=now_iso)
        logger.info("Signals refresh %s: %d buys across %d strategies in %.1fs (+%d tracked)",
                    mk, total_buys, len(latest["strategies"]), time.time() - t0, added)
        return {"updated": True, "market": mk, "signals_as_of": as_of,
                "total_buys": total_buys, "added_tracked": added,
                "elapsed_seconds": round(time.time() - t0, 1)}
    except Exception as e:
        logger.error("Signals refresh failed for %s: %s", mk, e, exc_info=True)
        signals_job_state.update(running=False, error=str(e),
                                 finished_at=datetime.now(timezone.utc).isoformat())
        return {"updated": False, "note": str(e)}
    finally:
        signals_job_state["running"] = False


def _downsample(series: pd.Series, points: int = 400) -> list[list]:
    """[ts_ms, value] pairs downsampled for charting."""
    if series is None or series.empty:
        return []
    step = max(1, len(series) // points)
    s = series.iloc[::step]
    return [[int(ts.timestamp() * 1000), round(float(v), 4)] for ts, v in s.items()]


def _serialize(
    results: list[dict],
    signals: dict,
    index: str,
    years: int,
    load_stats: dict,
    cost_bps: float,
    elapsed: float,
    market: str = "IN",
    benchmark_label: str = "NIFTY 50",
) -> dict:
    benchmark_equity = next((r["benchmark_equity"] for r in results if not r["benchmark_equity"].empty), pd.Series(dtype=float))
    by_id = {s.id: s for s in strategies_for_market(market)}
    for s in promoted_strategies(market):        # resolve promoted discovered strategies too
        by_id.setdefault(s.id, s)
    strategies_payload = []
    for r in results:
        strat = by_id.get(r["strategy_id"])
        if strat is None:
            continue
        m = r["metrics"]
        strategies_payload.append({
            "id": strat.id,
            "name": strat.name,
            "category": strat.category,
            "description": strat.description,
            "metrics": m,
            "stocks_traded": r["stocks_traded"],
            "open_positions": len(r["open_trades"]),
            "equity": _downsample(r["equity"]),
            "signals": signals.get(strat.id, {"buy": [], "sell": [], "holding": []}),
            "sample_trades": [
                {
                    "ticker": t.ticker, "entry_date": t.entry_date, "exit_date": t.exit_date,
                    "ret_pct": t.ret_pct, "holding_days": t.holding_days, "exit_reason": t.exit_reason,
                }
                for t in sorted(r["trades"], key=lambda t: t.ret_pct, reverse=True)[:5]
            ],
            "worst_trades": [
                {
                    "ticker": t.ticker, "entry_date": t.entry_date, "exit_date": t.exit_date,
                    "ret_pct": t.ret_pct, "holding_days": t.holding_days, "exit_reason": t.exit_reason,
                }
                for t in sorted(r["trades"], key=lambda t: t.ret_pct)[:5]
            ],
        })

    payload = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "index": index,
        "benchmark_label": benchmark_label,
        "years": years,
        "cost_roundtrip_bps": cost_bps,
        "elapsed_seconds": round(elapsed, 1),
        "data_stats": load_stats,
        "benchmark_equity": _downsample(benchmark_equity),
        "strategies": strategies_payload,
        "disclaimer": _bias_disclaimer(index),
    }
    # Strip inf/NaN (JSON-noncompliant) so persisted results + API responses stay clean.
    return deep_sanitize(payload)


def _persist(payload: dict, market: str = "IN") -> None:
    dest = _latest_file(market)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(dest)
    logger.info(f"Backtest results persisted to {dest}")


def load_latest(market: str = "IN") -> Optional[dict]:
    """Latest backtest payload for a market. IN falls back to the pre-multi-market
    file so an existing run keeps working after the upgrade."""
    path = _latest_file(market)
    if not path.exists():
        if (market or "IN").upper() == "IN" and _LEGACY_LATEST.exists():
            path = _LEGACY_LATEST
        else:
            return None
    try:
        # deep_sanitize heals non-JSON-compliant floats (inf/NaN → None) that a
        # degenerate strategy can produce (e.g. profit_factor / calmar with zero
        # losses or zero drawdown), which would otherwise 500 the API response.
        return deep_sanitize(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:
        logger.error(f"Could not read backtest results ({path.name}): {e}")
        return None


# ── Curated basket persistence (the committed anchor for day-to-day hysteresis) ──
# The live board is rebuilt on every load, but the basket must stay STABLE across intra-day refreshes
# and only advance once per run/track — so we persist the last committed basket per market and merge
# against it (basket.build_basket applies the hysteresis). See pipeline_core/backtest/basket.py.

def _basket_file(market: str = "IN"):
    return RESULTS_DIR / f"basket_{(market or 'IN').upper()}.json"


def load_basket(market: str = "IN") -> Optional[dict]:
    """The last committed curated basket for a market (the hysteresis anchor). None if none yet."""
    path = _basket_file(market)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Could not read basket ({path.name}): {e}")
        return None


def _save_basket(market: str, payload: Optional[dict]) -> None:
    """Commit a basket as the new hysteresis anchor (atomic write). No-op for an empty basket."""
    if not payload:
        return
    dest = _basket_file(market)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(deep_sanitize(payload)), encoding="utf-8")
    tmp.replace(dest)


# ── My Hold Basket persistence (the user's COMMITTED, conviction-held book) ──
# Unlike the daily anchor above (which just stabilises the live view), this is a book the user explicitly
# committed to: its items carry entry_price + committed_at, and it changes only on an explicit apply.

def _hold_basket_file(market: str = "IN"):
    return RESULTS_DIR / f"hold_basket_{(market or 'IN').upper()}.json"


def _hold_portfolio_name(market: str) -> str:
    return f"Hold Basket — {(market or 'IN').upper()}"


def load_hold_basket(market: str = "IN") -> Optional[dict]:
    """The user's committed hold basket for a market (source of truth for holdings). None if none yet."""
    path = _hold_basket_file(market)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Could not read hold basket ({path.name}): {e}")
        return None


def _save_hold_basket(market: str, payload: Optional[dict]) -> None:
    if not payload:
        return
    dest = _hold_basket_file(market)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(deep_sanitize(payload)), encoding="utf-8")
    tmp.replace(dest)


def _clear_hold_basket(market: str) -> None:
    _hold_basket_file(market).unlink(missing_ok=True)


# ── Result shaping (shared by the REST app and the MCP server) ──────

# Public sort keys → metric field in the persisted payload.
SORT_KEY_MAP = {
    "sharpe": "sharpe", "win_rate": "win_rate", "cagr": "cagr_pct",
    "max_drawdown": "max_drawdown_pct", "profit_factor": "profit_factor",
    "expectancy": "expectancy_pct", "calmar": "calmar",
    "alpha": "alpha_vs_benchmark_pct", "sortino": "sortino",
    "total_return": "total_return_pct",
}
SORT_KEYS = list(SORT_KEY_MAP)


# Union across markets so a persisted signal's id resolves regardless of which market produced it
# (crypto ideas carry crypto-native ids that aren't in the default STRATEGIES list).
_STRATEGY_BY_ID: dict[str, StrategyDef] = {s.id: s for s in ALL_STRATEGIES}


def _strategy_plan(strategy_id: str, market: str = "IN") -> dict:
    """The risk-control rules attached to a strategy (stop / target / trail / time stop).
    Used to turn a fresh BUY signal into an actionable trade idea with concrete levels.
    Falls back to a promoted discovered strategy when the id isn't a built-in."""
    s = _STRATEGY_BY_ID.get(strategy_id) or get_discovered_strategy(strategy_id, market)
    if s is None:
        return {}
    return {
        "stop_loss_pct": s.stop_loss_pct,
        "trailing_stop_pct": s.trailing_stop_pct,
        "target_pct": s.target_pct,
        "max_holding_days": s.max_holding_days,
        "min_bars": s.min_bars,
    }


def strategy_library(market: str = "IN") -> list[dict]:
    """Static catalog of every strategy — available even before a run. Includes user-promoted
    discovered strategies for the market (tagged discovered:true)."""
    lib = [
        {"id": s.id, "name": s.name, "category": s.category, "description": s.description,
         "plan": _strategy_plan(s.id), "discovered": False}
        for s in strategies_for_market(market)
    ]
    for s in promoted_strategies(market):
        lib.append({"id": s.id, "name": s.name, "category": s.category, "description": s.description,
                    "plan": _strategy_plan(s.id, market), "discovered": True})
    return lib


def leaderboard(sort_by: str = "sharpe", market: str = "IN") -> Optional[dict]:
    """All strategies with metrics + signal counts, sorted. None if no run yet."""
    latest = load_latest(market)
    if latest is None:
        return None

    rows = [
        {
            "id": s["id"],
            "name": s["name"],
            "category": s["category"],
            "description": s["description"],
            "open_positions": s["open_positions"],
            "signals_buy": len(s["signals"]["buy"]),
            "signals_sell": len(s["signals"]["sell"]),
            "signals_holding": len(s["signals"]["holding"]),
            **s["metrics"],
        }
        for s in latest["strategies"]
    ]

    key = SORT_KEY_MAP.get(sort_by, "sharpe")

    def sort_val(row):
        v = row.get(key)
        if v is None:
            return float("-inf")
        if v == "inf":
            return float("inf")
        return v  # max_drawdown is negative → less-negative sorts higher, as intended

    rows.sort(key=sort_val, reverse=True)
    return {
        "strategies": rows,
        "run_at": latest["run_at"],
        "market": latest.get("market", "IN"),
        "index": latest["index"],
        "benchmark_label": latest.get("benchmark_label", "NIFTY 50"),
        "years": latest["years"],
        "sort_by": sort_by,
        "data_stats": latest["data_stats"],
        "disclaimer": latest["disclaimer"],
    }


def strategy_detail(strategy_id: str, include_curves: bool = True, market: str = "IN") -> Optional[dict]:
    """Full detail for one strategy (metrics, signals, sample trades, equity).
    None if there is no run or the id isn't in it."""
    latest = load_latest(market)
    if latest is None:
        return None
    for s in latest["strategies"]:
        if s["id"] == strategy_id:
            detail = {
                **s,
                "run_at": latest["run_at"],
                "market": latest.get("market", "IN"),
                "benchmark_label": latest.get("benchmark_label", "NIFTY 50"),
                "years": latest["years"],
                "disclaimer": latest["disclaimer"],
                "plan": _strategy_plan(strategy_id, market),
            }
            if include_curves:
                detail["benchmark_equity"] = latest["benchmark_equity"]
            else:
                detail.pop("equity", None)
            return detail
    return None


def signals_summary(signal_type: Optional[str] = None, market: str = "IN") -> Optional[dict]:
    """Current BUY/SELL/HOLDING signals for every strategy. None if no run yet.
    signal_type ∈ {buy, sell, holding} narrows each strategy to that list."""
    latest = load_latest(market)
    if latest is None:
        return None
    kinds = [signal_type] if signal_type in ("buy", "sell", "holding") else ["buy", "sell", "holding"]
    return {
        "run_at": latest["run_at"],
        "strategies": [
            {
                "id": s["id"],
                "name": s["name"],
                "category": s["category"],
                "signals": {k: s["signals"][k] for k in kinds},
            }
            for s in latest["strategies"]
        ],
    }


# ── Trade ideas by horizon ─────────────────────────────────────────

HORIZON_META = {
    "short": {
        "label": "Short term",
        "sub": "Days to ~2 weeks",
        "detail": "Mean-reversion snapbacks and quick momentum/breakout setups — tight time "
                  "stops (≤ 15 days). Faster in, faster out.",
    },
    "medium": {
        "label": "Medium term",
        "sub": "~2 weeks to 2 months",
        "detail": "Momentum, breakout and volume setups with multi-week holds "
                  "(time stops 16–40 days).",
    },
    "long": {
        "label": "Long term",
        "sub": "Months — ride the trend",
        "detail": "Trend-following systems that exit on a trend/structure signal rather than a "
                  "fixed clock (golden cross, DMA regimes, Donchian, Supertrend).",
    },
}


# Fallback stop (fraction) when none of the flagging strategies define a hard stop —
# tighter for shorter horizons.
HORIZON_DEFAULT_STOP = {"short": 0.06, "medium": 0.09, "long": 0.13}

# Crypto is far more volatile than equities, so the equity-calibrated stops above get shaken out by
# normal noise (the tracked crypto book stopped out ~2× as often as it hit target). Crypto ideas use
# these wider floors instead, with proportionally wider targets so reward:risk stays sensible.
CRYPTO_HORIZON_STOP = {"short": 0.15, "medium": 0.22, "long": 0.30}
CRYPTO_HORIZON_TARGET = {"short": 0.25, "medium": 0.40, "long": 0.60}

# Fallback upside target (fraction) when none of the flagging strategies define a fixed
# profit target — trend-followers (long) ride further, so they aim higher.
HORIZON_DEFAULT_TARGET = {"short": 0.10, "medium": 0.18, "long": 0.30}

# Half-width (fraction) of the suggested accumulation zone around the latest close.
# Wider for longer horizons, where the exact entry price matters less.
HORIZON_BUY_BAND = {"short": 0.015, "medium": 0.025, "long": 0.04}

# An idea whose latest signal is this recent is badged "new" — a fresh setup, not one
# that's been sitting on the board for a while.
IDEA_NEW_DAYS = 3


def strategy_horizon(sdef: StrategyDef) -> str:
    """Classify a strategy into short / medium / long term by its holding horizon.
    A strategy with no time stop exits on a trend/structure signal → it rides the move,
    so it counts as long term."""
    mh = sdef.max_holding_days
    if mh is None:
        return "long"
    if mh <= 15:
        return "short"
    if mh <= 40:
        return "medium"
    return "long"


def _idea_stop(horizon: str, stop_pcts: list, entry, market: str = "IN", side: str = "long") -> tuple:
    """Every trade idea gets a stop. Equities prefer the *tightest* stop the flagging strategies
    define (hard or trailing). CRYPTO instead uses the *median* strategy stop floored at a
    crypto-volatility minimum (`CRYPTO_HORIZON_STOP`) — the equity-calibrated tight stops get shaken
    out by ordinary crypto noise, so taking the tightest one is exactly wrong there. A SHORT's stop
    sits ABOVE entry (price rising against the position)."""
    if str(market).upper() == "CRYPTO":
        floor = CRYPTO_HORIZON_STOP[horizon]
        stop_pct = max(statistics.median(stop_pcts), floor) if stop_pcts else floor
        source = "crypto-floor" if stop_pcts else "crypto-default"
    elif stop_pcts:
        stop_pct, source = min(stop_pcts), "strategy"
    else:
        stop_pct, source = HORIZON_DEFAULT_STOP[horizon], "horizon-default"
    sign = 1 if side == "short" else -1                       # short stop is above entry
    stop_price = round_price(entry * (1 + sign * stop_pct)) if entry else None
    return round(stop_pct, 4), stop_price, source


def _idea_target(horizon: str, target_pcts: list, entry, market: str = "IN", side: str = "long") -> tuple:
    """Upside for a trade idea. Prefer the average fixed profit target across the flagging strategies
    that define one; otherwise fall back to a horizon default. For CRYPTO the target is floored at a
    wider crypto minimum (`CRYPTO_HORIZON_TARGET`) so reward:risk stays sensible against the wider
    crypto stop. A SHORT's target sits BELOW entry (price falling in the position's favour).
    Returns (target_pct, target_price, source)."""
    is_crypto = str(market).upper() == "CRYPTO"
    if target_pcts:
        target_pct, source = sum(target_pcts) / len(target_pcts), "strategy"
    else:
        target_pct = (CRYPTO_HORIZON_TARGET if is_crypto else HORIZON_DEFAULT_TARGET)[horizon]
        source = "crypto-default" if is_crypto else "horizon-default"
    if is_crypto and target_pct < CRYPTO_HORIZON_TARGET[horizon]:
        target_pct, source = CRYPTO_HORIZON_TARGET[horizon], "crypto-floor"
    sign = -1 if side == "short" else 1                        # short target is below entry
    target_price = round_price(entry * (1 + sign * target_pct)) if entry else None
    return round(target_pct, 4), target_price, source


def _idea_buy_range(horizon: str, entry) -> tuple:
    """Suggested accumulation zone around the latest close — a band whose width scales with
    the horizon. Returns (buy_low, buy_high), or (None, None) with no price."""
    if not entry:
        return None, None
    band = HORIZON_BUY_BAND[horizon]
    return round_price(entry * (1 - band)), round_price(entry * (1 + band))


def _strategy_risk_ok(max_drawdown_pct, sharpe, market: str) -> bool:
    """Whether a strategy's backtested risk is good enough for its BUY signal to *count toward* a
    crypto idea's conviction. Only CRYPTO is gated — its strategies carry −74%…−100% drawdowns, so a
    coin flagged by a pile of near-wipeout strategies shouldn't read as high-conviction. Every equity
    strategy passes; missing metrics get the benefit of the doubt (pass)."""
    if str(market).upper() != "CRYPTO":
        return True
    if max_drawdown_pct is not None and max_drawdown_pct <= settings.crypto_idea_dd_floor:
        return False
    if sharpe is not None and sharpe < settings.crypto_idea_sharpe_floor:
        return False
    return True


def _crypto_idea_gate(cands: list, tilt: float, demote_map: Optional[dict] = None) -> list:
    """The crypto board gate, applied to one horizon's candidate pool.

    LONGS survive only if a non-catastrophic strategy backs them (``eligible_conviction`` ≥ 1) or the
    idea is carried (an open tracked position); the long side is never emptied — if every long is gated,
    they're all kept and simply rank last.

    SHORTS have no strategy that clears the backtest risk gate today (crypto shorts lose over a 10y
    sample), so a *proven* short is rare. But in a genuinely BEARISH tide a short still has some
    potential, so the best few unproven shorts are surfaced as **speculative** watchlist ideas —
    ``c["speculative"]=True``, ranked by raw bearish agreement (conviction → least-bad Sharpe → least-bad
    drawdown), capped at ``crypto_shorts_max_speculative`` — only when ``tilt`` ≤
    ``crypto_shorts_regime_tilt``. On a risk-on day (or with the flag off) unproven shorts are dropped."""
    def _eligible(c):
        return c.get("carried") or c.get("eligible_conviction", 0) >= 1

    longs = [c for c in cands if c.get("side", "long") != "short"]
    shorts = [c for c in cands if c.get("side") == "short"]
    kept_longs = [c for c in longs if _eligible(c)] or longs
    kept_shorts = [c for c in shorts if _eligible(c)]                 # proven shorts (cleared the gate)
    unproven = [c for c in shorts if not _eligible(c)]
    if unproven and settings.crypto_shorts_regime_surface and tilt <= settings.crypto_shorts_regime_tilt:
        if demote_map:                                               # a short PROVEN net-negative live never surfaces, even speculatively
            unproven = [c for c in unproven
                        if not _strategy_demote_reason(c.get("strategies"), demote_map)]
        unproven.sort(key=lambda c: (c.get("conviction", 0), c.get("avg_sharpe") or -9,
                      c.get("avg_max_drawdown") if c.get("avg_max_drawdown") is not None else -999),
                      reverse=True)
        for c in unproven[:settings.crypto_shorts_max_speculative]:
            c["speculative"] = True
            kept_shorts.append(c)
    return kept_longs + kept_shorts


def _days_since(date_str: Optional[str]) -> Optional[int]:
    """Whole days from an ISO date/datetime (assumed UTC) to now. None if missing/unparseable."""
    if not date_str:
        return None
    try:
        d = datetime.fromisoformat(str(date_str)[:19])
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - d).days)
    except Exception:
        return None


def _partition_tracked(market: str) -> tuple[dict, dict]:
    """From the tracker for this market: ({(ticker,horizon): latest close exit_date}, still-open
    ideas grouped by horizon). The board hides a closed idea UNLESS a fresh signal fired after that
    exit (a genuine **re-entry**, which resurfaces), and **carries** still-open ideas so a valid
    setup stays visible until it actually closes. A closed record with no exit_date is treated as
    blocking (a far-future sentinel) — conservative, so a re-entry needs a real, earlier exit."""
    mk = (market or "IN").upper()
    closed_exit: dict = {}
    open_by_h: dict[str, list] = {"short": [], "medium": [], "long": []}
    for t in _load_tracked():
        if (t.get("market") or "IN") != mk:
            continue
        key = (t.get("ticker"), t.get("horizon"), t.get("side", "long"))
        if t.get("closed"):
            ex = t.get("exit_date") or "9999-12-31"
            closed_exit[key] = max(closed_exit.get(key, ""), ex)
        elif t.get("horizon") in open_by_h:
            open_by_h[t["horizon"]].append(t)
    return closed_exit, open_by_h


def _carried_candidate(t: dict, wr_map: Optional[dict] = None, demote_map: Optional[dict] = None) -> dict:
    """A still-open tracked idea rendered as a board candidate (its stored, signal-anchored plan),
    so it persists on the board — ranked among fresh ideas by conviction/win-rate — until it
    closes. `carried=True` marks it as a held (not freshly re-signalled) idea. `wr_map` (strategy
    name → backtest win-rate) lets a carried idea (which stores only names) carry an edge-weighted
    conviction on the same scale as fresh ideas. `demote_map` rides the realized-edge gate onto a
    held idea too, so a position whose strategies have since proven live-losing is down-ranked/flagged."""
    sp, tp = t.get("stop_pct"), t.get("target_pct")
    strategies = [{"name": n} for n in (t.get("strategies") or [])]
    cand = {
        "ticker": t["ticker"],
        "side": t.get("side", "long"),
        "price": t.get("entry_price"),
        "buy_low": t.get("buy_low"),
        "buy_high": t.get("buy_high"),
        "stop_pct": sp,
        "stop_price": t.get("stop_price"),
        "stop_source": "tracked",
        "target_pct": tp,
        "target_price": t.get("target_price"),
        "target_source": "tracked",
        "risk_reward": round(tp / sp, 2) if sp and tp else None,
        "conviction": t.get("conviction") or 1,
        "avg_win_rate": t.get("avg_win_rate"),
        "avg_sharpe": None,
        "typical_drawdown_pct": t.get("typical_drawdown_pct"),   # band persisted at entry (snapshot)
        "deep_drawdown_pct": t.get("deep_drawdown_pct"),
        "latest_signal_date": t.get("signal_date"),
        "age_days": _days_since(t.get("signal_date")),
        "is_new": False,
        "carried": True,
        "strategies": strategies,
        "weighted_conviction": _weighted_conviction(strategies, wr_map, demote_map),
    }
    _tag_realized_demote(cand, demote_map)
    return cand


def _size_bucket(shown: list, tilt: float) -> dict:
    """Size a bucket's ideas as one ¼-Kelly book, applying two independent haircuts to the book's risk
    budget: the regime risk-off haircut (tilt<0 trims longs, never below regime_size_floor) and a
    cohort-demotion haircut (a persistently under-performing market/horizon rides at cohort_size_factor).
    Each idea is tagged with the factors applied; a risk-on, non-demoted book sizes normally (unchanged)."""
    regime_factor = (max(settings.regime_size_floor, 1 + settings.regime_size_weight * tilt)
                     if tilt < 0 else 1.0)
    demote_reason = next((c.get("demote_reason") for c in shown if c.get("demote_reason")), None)
    cohort_factor = settings.cohort_size_factor if demote_reason else 1.0
    size_factor = regime_factor * cohort_factor
    if size_factor < 1.0:
        bucket_sizing = sizing.size_book(
            shown, max_book_risk_pct=settings.sizing_max_book_risk_pct * size_factor)
    else:
        bucket_sizing = sizing.size_book(shown)
    for c in shown:
        if regime_factor < 1.0:
            c["regime_size_factor"] = round(regime_factor, 3)
        if cohort_factor < 1.0:
            c["cohort_size_factor"] = round(cohort_factor, 3)
    if regime_factor < 1.0:
        bucket_sizing["regime_size_factor"] = round(regime_factor, 3)
    if cohort_factor < 1.0:
        bucket_sizing["cohort_size_factor"] = round(cohort_factor, 3)
        bucket_sizing["cohort_demote_reason"] = demote_reason
    return bucket_sizing


def _oversold_state(frame) -> Optional[dict]:
    """Current oversold read for an equity long from its recent daily closes: RSI(14) and price vs the
    50-DMA. Returns {rsi, vs_sma50_pct, oversold} or None with too little history. `oversold` is True when
    the stock is genuinely pulled back (RSI below the floor OR below its 50-DMA) — the setups the closed
    ledger shows winning ~75% vs ~26% for extended entries. See settings.equity_oversold_gate_enabled."""
    if frame is None or getattr(frame, "empty", True) or len(frame) < 50:
        return None
    c = frame["Close"].astype(float)
    diff = c.diff()
    up = diff.clip(lower=0).rolling(14).mean().iloc[-1]
    dn = (-diff.clip(upper=0)).rolling(14).mean().iloc[-1]
    rsi = 100.0 if not dn else 100.0 - 100.0 / (1.0 + up / dn)
    px = float(c.iloc[-1])
    sma50 = float(c.rolling(50).mean().iloc[-1])
    vs = (px / sma50 - 1.0) * 100.0 if sma50 else 0.0
    oversold = (rsi < settings.oversold_rsi_max) or (vs < 0)
    return {"rsi": round(rsi, 1), "vs_sma50_pct": round(vs, 1), "oversold": bool(oversold)}


def _cohort_demote_reason(market: Optional[str], horizon: Optional[str]) -> Optional[str]:
    """A reason string if this (market, horizon) is a soft-demoted cohort — one the closed ledger shows
    persistently under-performing its advertised odds (crypto daily: 34.6% realized win carried by a few
    tail winners; medium/long horizons: broadly net-losing) — else None. Demoted cohorts keep showing
    but get a win-prob haircut + smaller size. See settings.cohort_demote_*."""
    if not settings.cohort_demote_enabled:
        return None
    reasons = []
    if str(market or "").upper() in {m.upper() for m in settings.cohort_demote_markets}:
        reasons.append(f"{str(market).lower()} daily")
    if horizon in settings.cohort_demote_horizons:
        reasons.append(f"{horizon} horizon")
    return " + ".join(reasons) if reasons else None


def _strategy_demote_map(market: Optional[str]) -> dict:
    """Realized-edge demote map (strategy name → win-rate haircut pp) from THIS market's closed ledger,
    or {} when the gate is off or the market is out of scope. The map feeds _weighted_conviction (equity
    down-rank) and _crypto_idea_gate (drop a live-losing speculative short). See
    idea_ranking.strategy_demote_map and settings.realized_strategy_gate_*."""
    if not settings.realized_strategy_gate_enabled:
        return {}
    if str(market or "").upper() not in {m.upper() for m in settings.realized_gate_markets}:
        return {}
    mk = (market or "IN").upper()
    closed = [t for t in _load_tracked()
              if (t.get("market") or "IN").upper() == mk and t.get("closed")
              and t.get("realized_return_pct") is not None]
    return idea_ranking.strategy_demote_map(closed)


def _strategy_demote_reason(strategies: Optional[list], demote_map: Optional[dict]) -> Optional[str]:
    """A short reason if this idea is backed by a realized-demoted strategy (proven net-negative live),
    else None. Drives the UI flag and the crypto speculative-short drop. Matches on strategy NAME, so a
    strategy renamed since its trades were logged simply re-accumulates a fresh live sample (no false hit)."""
    if not demote_map or not strategies:
        return None
    names = [s.get("name") if isinstance(s, dict) else s for s in strategies]
    hit = sorted({n for n in names if n in demote_map})
    return "weak live edge: " + ", ".join(hit) if hit else None


def _tag_realized_demote(cand: dict, demote_map: Optional[dict]) -> None:
    """Flag a candidate whose edge leans on a realized-demoted strategy — transparency only; the
    functional down-rank is the win-rate haircut already folded into its weighted_conviction."""
    reason = _strategy_demote_reason(cand.get("strategies"), demote_map)
    if reason:
        cand["realized_demoted"] = True
        cand["realized_demote_reason"] = reason


def _conviction_edge_weight(win_rate: Optional[float]) -> float:
    """A strategy's vote weight toward RANKING conviction, from its backtest win-rate: clipped-linear,
    0 at/below the floor rising to 1 at 100%. A low-win-rate strategy (equity momentum ~43%) contributes
    a fractional vote so it stops inflating a high-win-rate mean-reversion cluster's conviction. A missing
    win-rate is a neutral 0.5 (can't judge). See settings.conviction_edge_floor_pct."""
    if win_rate is None:
        return 0.5
    floor = settings.conviction_edge_floor_pct
    if win_rate <= floor:
        return 0.0
    return min(1.0, (win_rate - floor) / (100.0 - floor))


def _weighted_conviction(strategies: Optional[list], wr_map: Optional[dict] = None,
                         demote_map: Optional[dict] = None) -> float:
    """Edge-weighted conviction: the sum of each backing strategy's vote weight (_conviction_edge_weight
    of its backtest win-rate). Reads win_rate from the strategy dict, falling back to `wr_map` (name →
    backtest win-rate) for carried ideas that store only names — so fresh and held ideas rank on one scale.
    `demote_map` (strategy name → win-rate haircut pp, from the realized-edge gate) shaves a strategy
    PROVEN net-negative live before the ramp, shrinking/zeroing its vote — see
    idea_ranking.strategy_demote_map. None/empty ⇒ the exact prior behaviour."""
    total = 0.0
    for s in strategies or []:
        if isinstance(s, dict):
            name = s.get("name")
            wr = s.get("win_rate")
            if wr is None and wr_map is not None:
                wr = wr_map.get(name)
        else:
            name = s
            wr = wr_map.get(s) if wr_map is not None else None
        if wr is not None and demote_map:
            wr = max(0.0, wr - demote_map.get(name, 0.0))
        total += _conviction_edge_weight(wr)
    return round(total, 3)


def _ranking_conviction(c: dict, market: str) -> float:
    """The conviction the base ranking score uses. Equity markets in settings.conviction_edge_markets
    (when conviction_edge_weighting is on) rank on the EDGE-WEIGHTED conviction, so a momentum-contaminated
    cluster no longer outranks a clean high-win-rate one at the same raw count; every other case keeps raw
    conviction (prior ordering). Crypto is exempt — it trends, and _regime_rank_key handles its gated path."""
    if (settings.conviction_edge_weighting and market != "CRYPTO"
            and market in settings.conviction_edge_markets):
        return c.get("weighted_conviction", c["conviction"])
    return c["conviction"]


def _rank_win_prob(c: dict) -> Optional[float]:
    """The win probability used for RANKING and the edge gate: the calibrated/fusion-preferred estimate
    (sizing._win_prob — model > calibrated > advertised) when rank_use_calibrated is on, else the raw
    advertised rate. A demoted-cohort idea (c['demote_reason'], tagged by _recompose_bucket) is haircut
    by cohort_haircut_pp so more of them fail the edge floor. A fraction in [0, 1], or None if no win-rate."""
    if settings.rank_use_calibrated:
        p = sizing._win_prob(c)
    else:
        wr = c.get("avg_win_rate")
        p = wr / 100.0 if wr is not None else None
    if p is None:
        return None
    if c.get("demote_reason"):
        p = max(0.0, p - settings.cohort_haircut_pp / 100.0)
    return p


def _breakeven_win_prob(c: dict) -> Optional[float]:
    """The geometric breakeven win rate implied by an idea's own risk/reward: risking `stop_dist` to
    make `target_dist` breaks even at stop_dist / (stop_dist + target_dist). Uses the stored prices when
    present, else the advertised stop_pct/target_pct. None if the geometry is missing."""
    entry, stop = c.get("entry_price"), c.get("stop_price")
    target = c.get("target_price") or _effective_target(c)
    if entry and stop is not None and target is not None:
        stop_dist, target_dist = abs(entry - stop), abs(target - entry)
    else:
        sp, tp = c.get("stop_pct"), c.get("target_pct")
        if not sp or not tp:
            return None
        stop_dist, target_dist = sp, tp
    denom = stop_dist + target_dist
    return stop_dist / denom if denom > 0 else None


def _edge_slack(c: dict) -> float:
    """Win probability MINUS breakeven — how much room an idea clears its own risk/reward by. Used both
    to gate (must exceed the margin) and to rank the least-bad when a gate would otherwise empty."""
    wp, be = _rank_win_prob(c), _breakeven_win_prob(c)
    return (wp - be) if (wp is not None and be is not None) else -9.0


def _passes_edge_floor(c: dict) -> bool:
    """True unless the idea's (calibrated) win probability sits below its own breakeven plus the required
    margin. Ideas that carry no win-prob or no geometry pass — the floor never judges what it can't."""
    wp, be = _rank_win_prob(c), _breakeven_win_prob(c)
    if wp is None or be is None:
        return True
    return wp >= be + settings.idea_min_edge_margin_pp / 100.0


def _rank_pool(pool: list, max_per_bucket: int,
               clusters: Optional[dict], pillar_ctx: Optional[dict], market: Optional[str] = None) -> tuple:
    """Rank ONE direction's candidate pool by the composite score → quality gate → edge floor →
    zero-edge conviction floor → correlation de-dup → cap → optional pillar tiering. Returns
    (shown, quality_cut, edge_cut, conv_cut, dedup_info). Mutates candidate fields
    (rank_multiplier/rank_score/pillars/tier) but no bucket.
    Ranking uses the calibrated win probability (_rank_win_prob); the edge floor drops ideas whose
    win-prob can't clear their own breakeven. The regime tilt is already baked
    into each idea's `_base_score` (side-appropriate sign) and sizing is applied by the caller, so no
    tilt is needed here. A SHORT's directional soft factors are inverted (a bullish tilt is a headwind
    for it); for long candidates the inversion is a no-op, so behaviour is unchanged."""
    for c in pool:
        factors = {name: c.get(f"{name}_factor") for name in idea_ranking.FACTOR_NAMES}
        if c.get("side") == "short":
            # Directional factors flip for a short: accumulation / bullish news / a leading sector /
            # high quality all argue AGAINST it. Earnings is event-risk (non-directional) → kept.
            for name in ("news", "sector", "quality", "smart_money"):
                if factors.get(name) is not None:
                    factors[name] = -factors[name]
        mult = idea_ranking.composite_multiplier(factors)
        c["rank_multiplier"] = round(mult, 4)
        c["rank_score"] = round(c.get("_base_score", c.get("conviction", 0)) * mult, 6)
    pool.sort(key=lambda c: (c["rank_score"], _rank_win_prob(c) or 0, c.get("avg_sharpe") or 0),
              reverse=True)
    floor = settings.rank_min_multiplier
    survivors = [c for c in pool if c["rank_multiplier"] >= floor]
    quality_cut = len(pool) - len(survivors)
    # EDGE FLOOR — drop ideas whose (calibrated) win probability is below their own geometric breakeven
    # plus the required margin: an idea that can't clear its own risk/reward is the coin-flip the ledger
    # showed realizing ~32% win (net-negative). Never empties a bucket: if every survivor fails, keep the
    # least-bad few by edge slack, each flagged `below_edge`, so a thin book degrades gracefully (mirrors
    # the crypto gate's never-empty rule) rather than showing an empty column.
    # ZERO-EDGE CONVICTION FLOOR (equity) — a HARD filter: an idea whose edge-weighted conviction is ~0 has
    # NO backing strategy that clears the win-rate floor (e.g. an overbought Breakout-only long at RSI 85),
    # so it's removed outright. Unlike the breakeven floor below, this one MAY leave a bucket empty — an
    # honest empty column beats padding it with pure-noise garbage. Crypto exempt; a missing field is kept.
    eq_conv_floor = (market and settings.conviction_edge_weighting
                     and market in settings.conviction_edge_markets
                     and settings.min_weighted_conviction > 0)

    def _low_conv(c):
        wc = c.get("weighted_conviction")
        return bool(eq_conv_floor) and wc is not None and wc < settings.min_weighted_conviction

    with_edge = [c for c in survivors if not _low_conv(c)]
    conv_cut = len(survivors) - len(with_edge)
    survivors = with_edge
    # BREAKEVEN EDGE FLOOR — drop ideas whose (calibrated) win probability is below their own risk/reward
    # breakeven plus the margin (the coin-flips the ledger showed realizing ~32% win). Unlike the conv floor
    # this never empties a bucket: if every survivor fails, keep the least-bad few by edge slack, flagged
    # `below_edge`, so an edge-bearing but thin book degrades gracefully rather than showing an empty column.
    passed = [c for c in survivors if _passes_edge_floor(c)]
    edge_cut = len(survivors) - len(passed)
    if passed:
        survivors = passed
    elif survivors:
        survivors.sort(key=_edge_slack, reverse=True)
        survivors = survivors[:max(1, min(settings.edge_floor_min_keep, len(survivors)))]
        for c in survivors:
            c["below_edge"] = True
    # Correlation de-dup on the rank-sorted survivors (best-ranked idea per correlated cluster kept),
    # BEFORE the cap so independent names are promoted over correlated ones. Empty clusters → no-op.
    deduped, dd = dedup.dedup_book(survivors, clusters or {})
    shown = deduped[:max_per_bucket]
    # Multi-pillar confluence tiering (opt-in): tag each shown idea with its confirming families and
    # split actionable vs watch, then order actionable-first (stable → rank order kept within tier).
    if pillar_ctx is not None:
        avail = pillar_ctx.get("available", 0)
        mina = pillar_ctx.get("min_actionable")
        gate_shorts = not settings.crypto_shorts_actionable
        for c in shown:
            summ = idea_ranking.pillar_summary(c, avail, side=c.get("side", "long"))
            c["pillars"] = summ["pillars"]
            c["pillar_count"] = summ["count"]
            c["pillar_available"] = summ["available"]
            c["tier"] = idea_ranking.pillar_tier(summ["count"], avail, mina)
            # Crypto shorts stay on the WATCHLIST (visible, never "actionable") when either the cautious
            # flag is set OR the short is SPECULATIVE (surfaced by the bearish-regime path with no backtest
            # edge) — a proven short that cleared the risk gate can still go actionable.
            if (gate_shorts or c.get("speculative")) and c.get("side") == "short":
                c["tier"] = "watch"
                c["short_gated"] = True
        shown.sort(key=lambda c: 0 if c.get("tier") == "actionable" else 1)
    for c in pool:
        c.pop("_base_score", None)
    return shown, quality_cut, edge_cut, conv_cut, dd


def _recompose_bucket(bucket: dict, tilt: float, max_per_bucket: int,
                      clusters: Optional[dict] = None, pillar_ctx: Optional[dict] = None,
                      market: Optional[str] = None) -> None:
    """Re-rank a bucket's candidate POOL (shown + reserve) by the composite score — each idea's base
    conviction×regime score scaled by whatever soft factors (earnings/news/sector/quality) live
    enrichment has attached — then apply the QUALITY GATE, collapse CORRELATED duplicates, and keep
    the top `max_per_bucket`.

    Longs and shorts are ranked, gated, de-duped, capped and sized as INDEPENDENT books (a long and a
    correlated short are opposite bets, not duplicates; each side gets its own top-N). Non-crypto
    markets only ever have longs → the single-side path below is the exact prior behaviour.

    `pillar_ctx` ({available, min_actionable}) turns on MULTI-PILLAR TIERING: each shown idea is tagged
    with the independent families that confirm it (idea_ranking.idea_pillars) and split into an
    ACTIONABLE set (clears the confluence bar) vs a WATCHLIST, then stably re-ordered actionable-first
    (rank order preserved within each tier, so live-reprice positional patching still lines up). None
    (the default) skips tiering entirely, so existing callers/tests see the exact prior behaviour.

    The gate is the "quality over quantity" step: an idea whose soft factors collectively drag its
    multiplier below `rank_min_multiplier` (i.e. it's fighting multiple headwinds) is dropped rather
    than shown, so a bucket surfaces fewer, cleaner trades instead of a diluted top-N.

    De-dup is the "quantity over redundancy" step: given a symbol→cluster map (from correlated trailing
    returns), only the best-ranked idea per cluster is kept — so a top-N of, say, five mega-cap techs
    that all move together collapses to one, and independent reserve names get promoted in their place.
    With no factors attached every multiplier is 1.0 and with no clusters nothing is collapsed, so the
    order is exactly the base one (regression-safe)."""
    pool = bucket.get("candidates", []) + bucket.pop("_reserve", [])
    # Soft-demote tagging → the edge floor (via _rank_win_prob's haircut), sizing (_size_bucket) and the UI
    # badge all discount a demoted idea uniformly. Two reasons stack: (1) a persistently under-performing
    # (market, horizon) cohort; (2) an EXTENDED equity long (not oversold at entry — the closed ledger's
    # ~26%-win cohort, flagged `not_oversold` during enrich). A clean, oversold idea carries neither.
    cohort = _cohort_demote_reason(market, bucket.get("horizon"))
    for c in pool:
        reasons = [cohort] if cohort else []
        if c.get("not_oversold"):
            reasons.append("entered extended")
        if reasons:
            c["demoted"], c["demote_reason"] = True, " + ".join(reasons)
        else:                                          # clear any stale tag on re-rank (config toggled off)
            c.pop("demoted", None); c.pop("demote_reason", None)
    by_side: dict[str, list] = {}
    for c in pool:
        by_side.setdefault(c.get("side", "long"), []).append(c)

    if "short" not in by_side:
        # Long-only (every non-crypto market, and crypto with shorts off) — behaviour unchanged.
        shown, cut, edge_cut, conv_cut, dd = _rank_pool(pool, max_per_bucket, clusters, pillar_ctx, market)
        bucket["candidates"] = shown
        bucket["quality_cut"] = cut                       # conflicted ideas the multiplier gate removed
        bucket["edge_cut"] = edge_cut                     # ideas the breakeven edge floor removed
        bucket["conv_cut"] = conv_cut                     # zero-edge ideas (no strategy clears the win-rate floor)
        bucket["dedup"] = dd                              # {kept, dropped, collapsed_clusters}
        if pillar_ctx is not None:
            bucket["actionable_count"] = sum(1 for c in shown if c.get("tier") == "actionable")
            bucket["watch_count"] = sum(1 for c in shown if c.get("tier") == "watch")
        bucket["sizing"] = _size_bucket(shown, tilt)
        return

    # Mixed long+short (crypto): rank each side independently, then concatenate (longs first).
    combined, cut_total, edge_cut_total, conv_cut_total = [], 0, 0, 0
    dd_total = {"kept": 0, "dropped": 0, "collapsed_clusters": 0}
    for _side in ("long", "short"):
        if _side not in by_side:
            continue
        shown, cut, edge_cut, conv_cut, dd = _rank_pool(by_side[_side], max_per_bucket, clusters, pillar_ctx, market)
        combined += shown
        cut_total += cut
        edge_cut_total += edge_cut
        conv_cut_total += conv_cut
        dd_total["kept"] += dd.get("kept", 0)
        dd_total["dropped"] += dd.get("dropped", 0)
        dd_total["collapsed_clusters"] += dd.get("collapsed_clusters", 0)
    if pillar_ctx is not None:
        bucket["actionable_count"] = sum(1 for c in combined if c.get("tier") == "actionable")
        bucket["watch_count"] = sum(1 for c in combined if c.get("tier") == "watch")
    long_shown = [c for c in combined if c.get("side", "long") != "short"]
    short_shown = [c for c in combined if c.get("side") == "short"]
    bucket["candidates"] = combined
    bucket["quality_cut"] = cut_total
    bucket["edge_cut"] = edge_cut_total
    bucket["conv_cut"] = conv_cut_total
    bucket["dedup"] = dd_total
    bucket["sizing"] = _size_bucket(long_shown, tilt)
    bucket["short_sizing"] = _size_bucket(short_shown, -tilt) if short_shown else None


def horizon_ideas(max_per_bucket: int = 10, market: str = "IN") -> Optional[dict]:
    """Cross-strategy trade ideas grouped into short / medium / long term buckets.

    Each strategy's current BUY signals are pooled into its horizon bucket, then
    aggregated **by stock**: a stock flagged by more strategies in the same horizon is a
    higher-conviction idea. Each bucket is ranked by conviction (then average strategy
    quality) and capped at `max_per_bucket` candidates (hard-capped at 10 — more than that
    per horizon is impractical to track). None if there is no run yet.
    """
    latest = load_latest(market)
    if latest is None:
        return None

    max_per_bucket = max(1, min(int(max_per_bucket), 10))
    # Strategy name → backtest win-rate, so both fresh and carried ideas carry an edge-weighted
    # conviction on one scale (see _weighted_conviction / settings.conviction_edge_weighting).
    wr_map = {s["name"]: (s.get("metrics") or {}).get("win_rate") for s in latest["strategies"]}
    idea_market = (latest.get("market") or market or "IN").upper()
    # Realized-edge gate: strategies PROVEN net-negative in THIS market's closed ledger get their
    # effective win-rate shaved (equity: down-rank via weighted_conviction / zero-edge floor) or are
    # dropped from speculative crypto shorts. {} when the gate is off ⇒ the exact prior board. See
    # settings.realized_strategy_gate_* and idea_ranking.strategy_demote_map.
    demote_map = _strategy_demote_map(idea_market)
    # Calibrate advertised win-rates against realized ledger outcomes (isotonic). The curve (if any)
    # feeds each candidate a `calibrated_win_pct` that the sizer prefers over the raw backtest rate.
    cal = calibration.build_from_ledger(idea_market)
    cal_curve = cal.get("curve")
    # Learned fusion: a logistic model that combines conviction + win-rate + Sharpe into P(win),
    # active only when it beats the base rate out-of-sample. When active it supersedes calibration
    # in feeding the sizer (sizing._win_prob prefers model_win_pct).
    fusion_bundle = fusion.build_idea_model(idea_market)
    # Closed ideas (hit stop / target / time-stop) drop off the board (they're past trades) —
    # UNLESS a fresh signal fires after that close (a re-entry, which resurfaces). Still-open
    # tracked ideas are carried over so a valid setup isn't flushed when it isn't re-signaled.
    closed_exit, open_tracked = _partition_tracked(idea_market)
    open_keys = {(t["ticker"], h, t.get("side", "long")) for h, lst in open_tracked.items() for t in lst}
    horizons = ["short", "medium", "long"]
    # Pools are keyed by (side, ticker): the same coin can be both a long idea (from a long strategy)
    # and a short idea (from a short strategy) in the same horizon, tracked and shown independently.
    # Non-crypto markets only ever produce long strategies → every key is ("long", ticker), unchanged.
    allow_shorts = idea_market == "CRYPTO" and settings.crypto_shorts_enabled
    pools: dict[str, dict[tuple, dict]] = {h: {} for h in horizons}
    strat_by_horizon: dict[str, list] = {h: [] for h in horizons}

    for s in latest["strategies"]:
        sdef = _STRATEGY_BY_ID.get(s["id"])
        if sdef is None:
            continue
        side = getattr(sdef, "direction", "long")
        if side == "short" and not allow_shorts:      # honour the flag / market at surface time
            continue
        h = strategy_horizon(sdef)
        m = s.get("metrics", {}) or {}
        wr, shp, exp = m.get("win_rate"), m.get("sharpe"), m.get("expectancy_pct")
        dd = m.get("max_drawdown_pct")
        dip, deep = m.get("typical_dip_pct"), m.get("deep_dip_pct")   # winners' "normal drawdown" band
        eligible = _strategy_risk_ok(dd, shp, idea_market)   # crypto risk gate (equities always pass)
        strat_by_horizon[h].append({
            "id": s["id"], "name": s["name"], "category": s["category"],
            "win_rate": wr, "max_holding_days": sdef.max_holding_days,
        })
        for b in s["signals"]["buy"]:
            ticker = b["ticker"]
            agg = pools[h].setdefault((side, ticker), {
                "ticker": ticker, "side": side, "price": b.get("price"),
                "strategies": [], "signal_dates": set(),
                "_win_rates": [], "_sharpes": [], "_drawdowns": [], "_stop_pcts": [],
                "_target_pcts": [], "_n_eligible": 0, "_dips": [], "_deep_dips": [],
            })
            if agg["price"] is None:
                agg["price"] = b.get("price")
            agg["strategies"].append({
                "id": s["id"], "name": s["name"], "category": s["category"],
                "win_rate": wr, "sharpe": shp, "expectancy_pct": exp,
                "max_drawdown_pct": dd, "eligible": eligible,
                "signal_date": b.get("signal_date"),
            })
            if b.get("signal_date"):
                agg["signal_dates"].add(b["signal_date"])
            if wr is not None:
                agg["_win_rates"].append(wr)
            if shp is not None:
                agg["_sharpes"].append(shp)
            if dd is not None:
                agg["_drawdowns"].append(dd)
            if dip is not None:
                agg["_dips"].append(dip)
            if deep is not None:
                agg["_deep_dips"].append(deep)
            if eligible:
                agg["_n_eligible"] += 1
            for sp in (sdef.stop_loss_pct, sdef.trailing_stop_pct):
                if sp is not None:
                    agg["_stop_pcts"].append(sp)
            if sdef.target_pct is not None:
                agg["_target_pcts"].append(sdef.target_pct)

    # Market-regime tide — rank regime-aligned longs first and (in risk-off) trim size. Computed
    # once: it's a market-level gauge, identical across the three horizon buckets. A neutral tilt=0
    # reading (offline, gate off, or genuinely neutral) leaves ordering and sizing exactly as before.
    reg = market_regime.market_regime(idea_market) if settings.regime_gate_enabled else None
    tilt = reg["tilt"] if reg else 0.0

    # Fear & Greed mood — the display-only companion to the regime tide (per-market source: CNN for
    # US, alternative.me for crypto, a derived blend elsewhere). Rides along on the payload next to
    # `regime`; degrades to a neutral 50 so it never blocks the board.
    fng = market_sentiment.fear_greed(idea_market) if settings.fear_greed_enabled else None

    buckets = {}
    for h in horizons:
        cands = []
        fresh_keys = set()                          # (side, ticker) freshly signalled this scan
        for agg in pools[h].values():
            key = (agg["ticker"], h, agg["side"])
            latest_sig = max(agg["signal_dates"]) if agg["signal_dates"] else None
            exit_d = closed_exit.get(key)
            # Drop a closed idea only if it hasn't fired a fresh signal after its close and isn't
            # currently open — i.e. a stale past trade. A newer signal is a re-entry → keep it.
            if exit_d and key not in open_keys and not (latest_sig and latest_sig > exit_d):
                continue
            fresh_keys.add((agg["side"], agg["ticker"]))
            wins = agg["_win_rates"]
            sharpes = agg["_sharpes"]
            drawdowns = agg["_drawdowns"]
            # Survivorship honesty: the backtest universe is today's index members applied over history,
            # so advertised win-rates are inflated. Subtract a documented haircut (uniform → ordering
            # unchanged; only the shown/​sized win-prob drops) until a point-in-time universe lands.
            avg_wr = (max(0.0, round(sum(wins) / len(wins) - settings.survivorship_haircut_pp, 1))
                      if wins else None)
            avg_sh = round(sum(sharpes) / len(sharpes), 2) if sharpes else None
            avg_dd = round(sum(drawdowns) / len(drawdowns), 1) if drawdowns else None
            # "Normal drawdown" band: mean over the backing strategies of their winners' typical/deep dip
            # (engine.compute_metrics). Absent on pre-band persisted runs → None (band simply not shown).
            dips, deep_dips = agg["_dips"], agg["_deep_dips"]
            band_on = settings.drawdown_band_enabled
            typical_dd = round(sum(dips) / len(dips), 1) if (dips and band_on) else None
            deep_dd = round(sum(deep_dips) / len(deep_dips), 1) if (deep_dips and band_on) else None
            side = agg["side"]
            stop_pct, stop_price, stop_source = _idea_stop(h, agg["_stop_pcts"], agg["price"], idea_market, side)
            target_pct, target_price, target_source = _idea_target(h, agg["_target_pcts"], agg["price"], idea_market, side)
            buy_low, buy_high = _idea_buy_range(h, agg["price"])
            rr = round(target_pct / stop_pct, 2) if stop_pct else None
            age_days = _days_since(latest_sig)
            cand = {
                "ticker": agg["ticker"],
                "side": side,
                "price": agg["price"],
                "buy_low": buy_low,
                "buy_high": buy_high,
                "stop_pct": stop_pct,
                "stop_price": stop_price,
                "stop_source": stop_source,
                "target_pct": target_pct,
                "target_price": target_price,
                "target_source": target_source,
                "risk_reward": rr,
                "conviction": len(agg["strategies"]),
                # Conviction that survives the risk gate — the credible agreement (crypto drops votes
                # from near-wipeout strategies; equals `conviction` on equities). Ranking/gating use this.
                "eligible_conviction": agg["_n_eligible"],
                # Edge-weighted conviction — each vote scaled by its backtest win-rate, so a momentum-
                # contaminated cluster ranks below a clean high-win-rate one at the same raw count.
                "weighted_conviction": _weighted_conviction(agg["strategies"], wr_map, demote_map),
                "avg_win_rate": avg_wr,
                "avg_sharpe": avg_sh,
                "avg_max_drawdown": avg_dd,
                "typical_drawdown_pct": typical_dd,      # winners' median worst-dip ("normal drawdown")
                "deep_drawdown_pct": deep_dd,            # winners' 80th-pct worst-dip ("beyond normal")
                "latest_signal_date": latest_sig,
                "age_days": age_days,
                "is_new": age_days is not None and age_days <= IDEA_NEW_DAYS,
                "carried": False,
                "strategies": sorted(
                    agg["strategies"], key=lambda x: (x["win_rate"] or 0), reverse=True
                ),
            }
            _tag_realized_demote(cand, demote_map)
            cands.append(cand)
        # Carry over still-open tracked ideas not re-signalled this scan, so a good setup persists
        # on the board (ranked by win rate alongside fresh ones) until it actually closes. Skip any
        # with a non-positive entry price — legacy broken records that render as $0.00 cards and can
        # never close (they're purged separately); dropping them here keeps the board clean meanwhile.
        for t in open_tracked.get(h, []):
            if (t.get("side", "long"), t["ticker"]) not in fresh_keys and (t.get("entry_price") or 0) > 0:
                cands.append(_carried_candidate(t, wr_map, demote_map))
        # Crypto board gate (long risk gate + bearish-regime speculative-short surfacing). See
        # _crypto_idea_gate. Non-crypto markets skip it entirely (long-only, ungated).
        if idea_market == "CRYPTO":
            cands = _crypto_idea_gate(cands, tilt, demote_map)

        # Rank by conviction, then win rate (a not-closed idea with a higher win rate outranks a
        # fresh lower-win-rate one and stays within the cap), then Sharpe. The market regime softly
        # scales each idea's conviction by its OWN cyclicality × the market tilt: in risk-off a
        # trend/momentum long is trimmed while a mean-reversion long is cushioned, so a safer idea
        # can outrank a high-conviction counter-trend one. tilt=0 ⇒ factor 1 ⇒ ordering unchanged.
        # Crypto ranks on the risk-GATED conviction and leads ties with Sharpe (risk-adjusted quality)
        # then the least-bad drawdown, so a few solid strategies beat a pile of near-wipeout ones.
        def _eff_tilt(c):
            # A risk-off tide (tilt<0) is a HEADWIND for a long but a TAILWIND for a short, so shorts
            # read the regime with the opposite sign. tilt=0 ⇒ 0 either way ⇒ ordering unchanged.
            return -tilt if c.get("side") == "short" else tilt

        def _regime_rank_key(c):
            t = _eff_tilt(c)
            cyc = market_regime.idea_cyclicality([s.get("category") for s in c.get("strategies", [])])
            if idea_market == "CRYPTO":
                # Speculative shorts carry eligible_conviction 0 (their strategies fail the risk gate);
                # rank them by their RAW bearish agreement so the strongest setup leads the subgroup.
                conv = c["conviction"] if c.get("speculative") else c.get("eligible_conviction", c["conviction"])
                return (conv * (1 + settings.regime_rank_weight * t * cyc),
                        c["avg_sharpe"] or 0, c["avg_win_rate"] or 0,
                        c["avg_max_drawdown"] if c.get("avg_max_drawdown") is not None else -999)
            # Equity markets rank on EDGE-WEIGHTED conviction (each vote scaled by its backtest win-rate)
            # so a momentum-contaminated cluster no longer outranks a clean high-win-rate one at the same
            # raw count — raw conviction was an actively harmful ranker (IC -0.31 US). See _ranking_conviction.
            return (_ranking_conviction(c, idea_market) * (1 + settings.regime_rank_weight * t * cyc),
                    c["avg_win_rate"] or 0, c["avg_sharpe"] or 0)

        # Rank, pool and cap each DIRECTION independently, so a horizon surfaces up to max_per_bucket
        # longs AND up to max_per_bucket shorts. Longs lead the combined list (the UI renders the two
        # as separate subgroups). Non-crypto markets have only longs → identical to the prior board.
        pool_size = max(max_per_bucket, max_per_bucket * max(1, settings.rank_pool_mult))
        shown, reserve = [], []
        for _side in ("long", "short"):
            side_cands = [c for c in cands if c.get("side", "long") == _side]
            if not side_cands:
                continue
            side_cands.sort(key=_regime_rank_key, reverse=True)
            # Keep a re-rank POOL a few deep past the cap: live enrichment (earnings/news/…) can then
            # promote a cleaner idea into the shown top-N. Each pooled idea carries its base score for
            # that composite re-rank; the non-live board is simply the base top-N of the pool.
            side_pool = side_cands[:pool_size]
            for c in side_pool:
                c["_base_score"] = _regime_rank_key(c)[0]      # conviction × regime multiplier
            # Per-idea fields (regime fit / calibration / fusion) — set on the whole pool, so a reserve
            # idea promoted during live re-rank still carries them. All order-independent.
            if tilt:                               # tag how each idea sits vs the tide (skipped when neutral)
                for c in side_pool:
                    cyc = market_regime.idea_cyclicality([s.get("category") for s in c.get("strategies", [])])
                    pull = _eff_tilt(c) * cyc      # >0 = the tide favours this idea, <0 = against it
                    c["regime_fit"] = ("tailwind" if pull > 0.05 else
                                       "headwind" if pull < -0.05 else "neutral")
            if cal_curve:
                for c in side_pool:
                    if c.get("avg_win_rate") is not None:
                        c["calibrated_win_pct"] = calibration.apply_calibration(cal_curve, c["avg_win_rate"])
            if fusion_bundle.get("active"):
                for c in side_pool:
                    mp = fusion.model_win_pct(fusion_bundle, c)
                    if mp is not None:
                        c["model_win_pct"] = mp
            shown += side_pool[:max_per_bucket]
            reserve += side_pool[max_per_bucket:]
        # Size each direction as its own ¼-Kelly book with the side-appropriate regime tilt sign
        # (a risk-off haircut trims longs, not shorts). The UI reads per-idea alloc/risk off each.
        long_shown = [c for c in shown if c.get("side", "long") != "short"]
        short_shown = [c for c in shown if c.get("side") == "short"]
        buckets[h] = {
            "horizon": h,
            **HORIZON_META[h],
            "candidates": shown,
            "_reserve": reserve,                   # consumed by enrich_ideas_live's composite re-rank
            "sizing": _size_bucket(long_shown, tilt),
            "short_sizing": _size_bucket(short_shown, -tilt) if short_shown else None,
            "total_candidates": len(cands),
            "strategies": strat_by_horizon[h],
            "strategy_count": len(strat_by_horizon[h]),
        }

    as_of = None
    if latest["strategies"]:
        as_of = (latest["strategies"][0]["signals"] or {}).get("as_of")

    cal_public = {"active": cal["active"], "closed": cal["closed"], "decided": cal["decided"],
                  "reliability_by_conviction": cal["reliability_by_conviction"],
                  "min_samples": cal["min_samples"]}
    if cal_curve:
        cal_public.update(brier_raw=cal_curve["brier_raw"], brier_cal=cal_curve["brier_cal"],
                          n=cal_curve["n"])
    # Crypto risk disclosure — its strategies backtest with deep drawdowns, so make that explicit
    # rather than implied. Surfaces the avg backtested drawdown of the strategies behind the shown
    # ideas + how they're gated/ranked. None (hidden) for equity markets.
    risk_note = None
    if idea_market == "CRYPTO":
        dds = [c["avg_max_drawdown"] for b in buckets.values() for c in b.get("candidates", [])
               if c.get("avg_max_drawdown") is not None]
        avg_txt = (f"Backing strategies average {round(sum(dds) / len(dds), 1)}% max drawdown. "
                   if dds else "")
        funding = latest.get("funding_bps_per_day")
        fund_txt = (f" Shorts also pay ~{funding:g} bps/day funding while held."
                    if funding else "")
        risk_note = (f"{avg_txt}Crypto ideas come from equity strategies with deep backtested "
                     f"drawdowns — those worse than {settings.crypto_idea_dd_floor:g}% are filtered, "
                     "the rest ranked by risk-adjusted quality, with wider crypto stops. Size small; "
                     f"decision-support, not advice.{fund_txt}")
    # Board-level honesty disclosure (all markets): the realistic cost the backtest was priced at + the
    # survivorship haircut applied to advertised win-rates. Lets the UI show "priced with X bps · win
    # rates haircut Ypp for survivorship" so the numbers don't over-promise.
    cost_disclosure = {
        "cost_bps": latest.get("cost_roundtrip_bps"),
        "funding_bps_per_day": latest.get("funding_bps_per_day") or 0.0,
        "survivorship_haircut_pp": settings.survivorship_haircut_pp,
    }
    return {
        "horizons": horizons,
        "buckets": buckets,
        "sizing_params": sizing.sizing_params(),
        "ranking": idea_ranking.rank_weights(),
        "pillars": idea_ranking.pillar_disclosure(idea_market),
        "calibration": cal_public,
        "fusion": fusion.public_view(fusion_bundle),
        "regime": reg,
        "fear_greed": fng,
        "risk_note": risk_note,
        "cost_disclosure": cost_disclosure,
        "max_per_bucket": max_per_bucket,
        "run_at": latest["run_at"],
        "market": latest.get("market", "IN"),
        "index": latest["index"],
        "benchmark_label": latest.get("benchmark_label", "NIFTY 50"),
        "years": latest["years"],
        "as_of": as_of,
        "signals_refreshed_at": latest.get("signals_refreshed_at"),
        "signals_as_of": latest.get("signals_as_of") or as_of,
        "disclaimer": latest["disclaimer"],
    }


# Buy-zone status of the live price vs the idea's (signal-anchored) plan.
def _idea_zone(live: float, c: dict) -> str:
    stop, target = c.get("stop_price"), c.get("target_price")
    lo, hi = c.get("buy_low"), c.get("buy_high")
    if c.get("side") == "short":                      # short: stop above entry, target below
        if stop is not None and live >= stop:
            return "below_stop"                       # stopped out — price ran up through the stop
        if target is not None and live <= target:
            return "at_target"                        # short reached its (lower) target
    else:
        if stop is not None and live <= stop:
            return "below_stop"
        if target is not None and live >= target:
            return "at_target"
    if lo is not None and live < lo:
        return "below_zone"
    if hi is not None and live > hi:
        return "above_zone"
    return "in_zone"


async def enrich_ideas_live(ideas: Optional[dict], market: str = "IN") -> Optional[dict]:
    """Add a LIVE price to every board candidate without touching its generated plan.

    The buy/target/stop the idea was generated with stay exactly as-is; we add `live_price`
    (fresh 90s quote), `live_change_pct` (vs the signal price), a `zone` status (in/above/below
    the buy zone, at target, or stopped), and `live_levels` — the same plan **recomputed from the
    live price** (for an 'enter from here' view). Prices are fetched concurrently and cached."""
    if not ideas:
        return ideas
    from pipeline_core.markets import get_market
    from pipeline_core.services import earnings as earnings_svc, market_sentiment, sector as sector_svc
    mk = (ideas.get("market") or market or "IN").upper()
    max_per_bucket = ideas.get("max_per_bucket", 10)
    tilt = (ideas.get("regime") or {}).get("tilt") or 0.0
    is_equity = get_market(mk).kind == "equity"
    want_earnings = is_equity                             # metals/crypto have no earnings
    want_news = is_equity and settings.rank_w_news > 0    # news fetch only when the factor is on
    want_sector = settings.rank_w_sector > 0 and sector_svc.has_sectors(mk)
    want_quality = is_equity and settings.rank_w_quality > 0
    want_info = want_sector or want_quality               # one get_stock_info fetch feeds both
    want_dedup = settings.dedup_daily_enabled             # daily correlation de-dup (needs price history)
    want_entry_state = is_equity and settings.equity_oversold_gate_enabled  # oversold read for the entry gate

    # Multi-pillar confluence — how many independent families CAN confirm an idea in this market (data
    # sources switched on). Clamps the actionable bar (idea_ranking.pillar_tier) so thin-pillar markets
    # still surface actionable ideas. Regime counts only under a directional tide — a neutral tilt sets
    # no regime_fit, so it can't confirm anything and shouldn't inflate the denominator.
    available_families = (
        1                                    # technical (always — the idea exists because a strategy fired)
        + (1 if want_quality else 0)         # fundamental quality
        + (1 if smart_money.has_family(mk) else 0)  # smart-money family (13F/ownership; COT/flows per market)
        + (1 if tilt else 0)                 # regime tailwind (directional tide only)
        + (1 if want_news else 0)            # news sentiment
        + (1 if want_sector else 0)          # sector rotation
    )
    # Crypto uses a lower actionable bar (only ~3 families exist there, so the equity bar of 3 files
    # every idea under watch); every other market uses the standard bar. Still clamped to availability.
    min_actionable = (settings.pillar_min_actionable_crypto if mk == "CRYPTO"
                      else settings.pillar_min_actionable)
    pillar_ctx = ({"available": available_families, "min_actionable": min_actionable}
                  if settings.pillar_tiering_enabled else None)

    # The re-rank POOL = each bucket's shown candidates + its reserve (a reserve idea can be promoted
    # into the shown top-N by the live composite factors), so enrich the whole pool.
    def _pool(h):
        b = ideas["buckets"][h]
        return b.get("candidates", []) + b.get("_reserve", [])

    tickers = {c["ticker"] for h in ideas.get("horizons", []) for c in _pool(h)}
    sem = asyncio.Semaphore(_PRICE_FETCH_CONCURRENCY)

    async def _enrich(tk: str):
        async with sem:
            # Live quote (90s cache) + smart-money signal (1-day) + days-to-earnings (12h) +
            # news sentiment (~1h) + the ticker's sector (1-day, for rotation). The slow-moving ones
            # are cache hits on every auto-refresh; news/sector are fetched only when their factor is on.
            quote = await asyncio.to_thread(get_quote, tk, mk)
            own = await asyncio.to_thread(smart_money.get_signal, mk, tk)
            dte = await asyncio.to_thread(earnings_svc.days_to_earnings, tk, mk) if want_earnings else None
            news = await market_sentiment.news_sentiment(tk, None, mk) if want_news else None
            info = await asyncio.to_thread(get_stock_info, tk, mk) if want_info else None
            # 6mo of daily closes for the correlation de-dup + the oversold entry gate (cached; often
            # warm from the backtest run). One fetch feeds both.
            hist = await aget_price_history(tk, "6mo", "1d", mk) if (want_dedup or want_entry_state) else None
            return tk, quote, own, dte, news, info, hist

    fetched = await asyncio.gather(*(_enrich(t) for t in tickers)) if tickers else []
    quotes = {tk: q for tk, q, *_ in fetched}
    ownership = {tk: o for tk, _, o, *_ in fetched}
    days_earn = {tk: d for tk, _, _, d, *_ in fetched}
    news_by = {tk: n for tk, _, _, _, n, *_ in fetched}
    info_by = {tk: i for tk, _, _, _, _, i, _ in fetched}
    # Cluster the whole board's tickers once (correlation is horizon-independent); each bucket's
    # de-dup then keeps the best-ranked idea per cluster. No usable history → {} → de-dup is a no-op.
    frames_by_sym = {tk: h for tk, _, _, _, _, _, h in fetched if h is not None and not getattr(h, "empty", True)}
    clusters = dedup.cluster_symbols(frames_by_sym, enabled=want_dedup)
    # Sector relative strength is per-sector, so compute it once per distinct sector (cached).
    rs_by_sector = {sec: sector_svc.sector_rs(sec, mk)
                    for sec in {(i or {}).get("sector") for i in info_by.values() if i}
                    if sec} if want_sector else {}

    for h in ideas.get("horizons", []):
        band = HORIZON_BUY_BAND.get(h, 0.02)
        for c in _pool(h):
            own = ownership.get(c["ticker"])
            c["smart_money"] = own if own and own.get("available") else None
            # Back-compat: the 🏦 badge and factor log still read `inst_ownership` for the ownership
            # family (edgar-13f / yfinance snapshot); native sources (COT/flows/deals) live only on
            # `smart_money`. The normalized signal is a superset, so the badge renders unchanged.
            c["inst_ownership"] = (own if own and own.get("available")
                                   and own.get("source") in ("edgar-13f", "yfinance") else None)
            # Smart-money rank tilt: accumulation lifts the idea, distribution trims it (scaled by the
            # signal's conviction). Same normalized signal the badge/coattail already use — no new fetch.
            c["smart_money_factor"] = idea_ranking.smart_money_factor(c["smart_money"])
            # Sell-side visibility: surface DISTRIBUTION (the pros trimming / whales net-short) as a flag —
            # an AVOID warning on a long, a CONFIRMATION on a crypto short. The rank impact is already
            # applied above (+ the short-side inversion in _rank_pool); this only makes it visible.
            if settings.smart_money_distribution_enabled:
                c["smart_money_distributing"] = idea_ranking.distribution_flag(
                    c["smart_money"], c.get("side", "long"))
            # Earnings event risk: days to the next report + a soft rank penalty that ramps up inside
            # the window (idea_ranking.earnings_factor). None outside the window / non-equity markets.
            dte = days_earn.get(c["ticker"])
            c["days_to_earnings"] = dte
            c["earnings_factor"] = idea_ranking.earnings_factor(dte)
            # News sentiment: a soft ± re-rank (bullish lifts, bearish trims). Live-only, flagged.
            nw = news_by.get(c["ticker"])
            if nw:
                c["news_score"] = nw.get("score")
                c["news_consensus"] = nw.get("consensus")
                c["news_factor"] = idea_ranking.news_factor(nw.get("score"))
            info = info_by.get(c["ticker"])
            # Sector rotation: lift ideas whose sector leads the benchmark, trim laggards.
            sec = (info or {}).get("sector")
            if sec:
                rs = rs_by_sector.get(sec)
                c["sector"] = sec
                c["sector_rs"] = rs
                c["sector_factor"] = idea_ranking.sector_factor(rs)
            # Fundamental quality: lift high-ROE / low-leverage / healthy-margin names, trim weak ones.
            if want_quality and info:
                qs = idea_ranking.quality_score(info)
                c["quality_score"] = qs
                c["quality_factor"] = idea_ranking.quality_factor(qs)
            # Equity oversold entry gate: flag an EXTENDED (not pulled-back) long — the closed ledger shows
            # those realizing ~26% win vs ~75% for genuinely oversold entries. Longs only (a short wants the
            # opposite); the flag is folded into the demote reason in _recompose_bucket (win-prob haircut →
            # edge floor + smaller size). None when history is thin, so the gate never fires blind.
            if want_entry_state and c.get("side", "long") != "short":
                ov = _oversold_state(frames_by_sym.get(c["ticker"]))
                if ov is not None:
                    c["entry_rsi"] = ov["rsi"]
                    c["entry_vs_sma50"] = ov["vs_sma50_pct"]
                    c["oversold"] = ov["oversold"]
                    c["not_oversold"] = not ov["oversold"]
            live = quotes.get(c["ticker"])
            c["live_price"] = live
            if live is None:
                c["live_change_pct"] = c["zone"] = c["live_levels"] = None
                continue
            entry = c.get("price")
            c["live_change_pct"] = round((live - entry) / entry * 100, 2) if entry else None
            c["zone"] = _idea_zone(live, c)
            sp, tp = c.get("stop_pct"), c.get("target_pct")
            s_sign = 1 if c.get("side") == "short" else -1    # short stop is above, target below
            c["live_levels"] = {                              # same fractions, re-anchored to live
                "buy_low": round_price(live * (1 - band)),
                "buy_high": round_price(live * (1 + band)),
                "stop_price": round_price(live * (1 + s_sign * sp)) if sp else None,
                "target_price": round_price(live * (1 - s_sign * tp)) if tp else None,
                "risk_reward": round(tp / sp, 2) if sp and tp else c.get("risk_reward"),
            }
        # Composite re-rank the enriched pool (base conviction×regime × the soft factors), collapse
        # correlated duplicates, keep the top max_per_bucket, tier by pillar confluence, re-size. With
        # every factor neutral and no clusters this reproduces the base top-N (tiering is additive).
        _recompose_bucket(ideas["buckets"][h], tilt, max_per_bucket, clusters, pillar_ctx, mk)

    # Distil the enriched board into ONE curated cross-horizon basket. Read-only: it merges against the
    # last committed basket (hysteresis) but does NOT write here, so intra-day auto-refreshes stay stable
    # against the same anchor — committing happens once per run/track in snapshot_current_ideas.
    if settings.basket_enabled:
        ideas["basket"] = basket.build_basket(ideas, clusters, load_basket(mk))
        # The FRESH hold basket (today's ideal, staying-power tilt) — the target the committed My Hold
        # Basket is compared against. Read-only; committing/applying persist it explicitly.
        if settings.hold_basket_enabled:
            ideas["hold_basket"] = basket.build_basket(
                ideas, clusters, load_hold_basket(mk), hold_mode=True)
        # The COATTAIL basket — the daily basket filtered to names the smart money is accumulating
        # (services/smart_money). Read-only ride-along; "what to buy that the pros are buying too".
        if settings.coattail_basket_enabled:
            ideas["coattail_basket"] = basket.build_basket(ideas, clusters, coattail_mode=True)
        # The sell-side mirror — the COATTAIL SHORT sleeve: SHORT ideas the smart money is DISTRIBUTING
        # (crypto whales net-short). Read-only ride-along; "the short book the pros are positioned for".
        # Empty on long-only markets / risk-on tides (no shorts to curate) — honest by construction.
        if settings.coattail_short_basket_enabled:
            ideas["coattail_short_basket"] = basket.build_basket(ideas, clusters, distribute_mode=True)

    # The BEST-ODDS board — the same board's fresh setups ranked purely by win probability (top N).
    # Read-only ride-along; "which trades are most likely to win". Independent of basket_enabled.
    if settings.best_odds_enabled:
        ideas["best_odds"] = basket.build_best_odds(ideas, clusters)

    ideas["live_priced_at"] = datetime.now(timezone.utc).isoformat()
    return ideas


# ── Idea tracker (win / loss over time) ────────────────────────────

TRACKED_FILE = RESULTS_DIR / "tracked_ideas.json"
_PRICE_FETCH_CONCURRENCY = 8


def _load_tracked() -> list:
    if not TRACKED_FILE.exists():
        return []
    try:
        data = json.loads(TRACKED_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_tracked(items: list) -> None:
    tmp = TRACKED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items), encoding="utf-8")
    tmp.replace(TRACKED_FILE)


def backfill_ledger_costs() -> dict:
    """One-time, idempotent migration: rewrite already-closed tracked ideas that were recorded GROSS
    (before ledger costs landed) to NET, preserving the raw move as `gross_return_pct`. Records already
    carrying `_cost_adjusted` are skipped, so this is safe to re-run. Returns {adjusted, total}."""
    items = _load_tracked()
    n = 0
    for it in items:
        if not it.get("closed") or it.get("_cost_adjusted"):
            continue
        gross = it.get("realized_return_pct")
        if gross is None:
            continue
        cost = _roundtrip_cost_pct(it, it.get("bars_held") or 0)
        it["gross_return_pct"] = gross
        it["realized_return_pct"] = round(gross - cost, 2)
        it["cost_pct"] = round(cost, 3)
        it["_cost_adjusted"] = True
        n += 1
    if n:
        _save_tracked(items)
    return {"adjusted": n, "total": len(items)}


async def snapshot_current_ideas(max_per_bucket: int = 10, market: str = "IN") -> dict:
    """Record the current horizon trade ideas so their outcome can be followed over time.
    Dedup is per OPEN cycle (ticker, horizon, market): a name that's already open, or whose most
    recent close is at/after this signal, is skipped — but a fresh signal that fires AFTER a close
    is a **re-entry** and gets its own new tracked record (leaving the prior one in past trades).
    Returns {added, total}. Called at the end of every backtest run and on demand from the UI.

    The board is **live-enriched before snapshotting** so each tracked record captures the soft
    factors (earnings/news/sector/quality/regime) it was generated with — the entry side of the
    factor-attribution ledger. Enrichment degrades to neutral/None offline, so tracking never blocks;
    it also means we track the *shown* set (post quality-gate + de-dup), matching the live board."""
    ideas = horizon_ideas(max_per_bucket, market)
    if ideas is None:
        return {"added": 0, "total": len(_load_tracked()), "note": "no backtest yet"}
    ideas = await enrich_ideas_live(ideas, market)   # attach live factors before we persist them
    tracked = _load_tracked()
    idea_market = ideas.get("market", (market or "IN").upper())
    now = datetime.now(timezone.utc).isoformat()
    as_of = ideas.get("as_of")
    # Currently-open cycles (skip) and the latest close per name (a later signal re-enters).
    # Keys include `side` so a long and a short of the same coin+horizon are tracked independently.
    open_keys = {(t["ticker"], t["horizon"], (t.get("market") or "IN"), t.get("side", "long"))
                 for t in tracked if not t.get("closed")}
    last_exit: dict = {}
    for t in tracked:
        if t.get("closed"):
            k = (t["ticker"], t["horizon"], (t.get("market") or "IN"), t.get("side", "long"))
            last_exit[k] = max(last_exit.get(k, ""), t.get("exit_date") or "9999-12-31")
    # Entry rank as a within-cohort percentile (1.0 = best in its horizon+side, 0.0 = worst), captured
    # so idea_ranking.rank_efficiency can later measure whether rank predicted realized return. Candidates
    # are already ordered best-first per (horizon, side) by _rank_pool, so position = rank.
    rank_pct_by: dict = {}
    for h in ideas["horizons"]:
        cands = ideas["buckets"][h].get("candidates", [])
        for _side in ("long", "short"):
            grp = [c for c in cands if c.get("side", "long") == _side]
            n_grp = len(grp)
            for i, c in enumerate(grp):
                rank_pct_by[(c["ticker"], h, _side)] = round(1.0 - (i / (n_grp - 1) if n_grp > 1 else 0.0), 4)
    added = 0
    for h in ideas["horizons"]:
        for c in ideas["buckets"][h]["candidates"]:
            key = (c["ticker"], h, idea_market, c.get("side", "long"))
            sig = c["latest_signal_date"] or as_of
            if key in open_keys:
                continue                               # already open — same live idea
            if key in last_exit and not (sig and sig > last_exit[key]):
                continue                               # same closed cycle, not a re-entry
            entry_px = c.get("price") or c.get("live_price")
            if not entry_px or entry_px <= 0:
                continue                               # never track a priceless idea — it can
                                                       # never stop/target out and would carry forever
            tracked.append({
                "ticker": c["ticker"],
                "market": idea_market,
                "horizon": h,
                "side": c.get("side", "long"),         # "long" | "short" — drives P&L / stop-target sign
                "entry_price": entry_px,
                "buy_low": c.get("buy_low"),
                "buy_high": c.get("buy_high"),
                "stop_pct": c["stop_pct"],
                "stop_price": c["stop_price"],
                "target_pct": c.get("target_pct"),
                "target_price": c.get("target_price"),
                "conviction": c["conviction"],
                "avg_win_rate": c["avg_win_rate"],
                # Entry rank (composite score + within-cohort percentile) → the rank-efficiency north-star.
                "rank_score": c.get("rank_score"),
                "entry_rank_pct": rank_pct_by.get((c["ticker"], h, c.get("side", "long"))),
                # "Normal drawdown" band captured at entry so a held idea can be checked within/beyond it.
                "typical_drawdown_pct": c.get("typical_drawdown_pct"),
                "deep_drawdown_pct": c.get("deep_drawdown_pct"),
                # Edge-repair entry state — the "entry side" for validating the ranking/gate changes once
                # this idea closes: the edge-weighted conviction that ranked it, plus the oversold read
                # (RSI / vs-50-DMA) and demote reason that gated it. Neutral/None offline or gate-off.
                "weighted_conviction": c.get("weighted_conviction"),
                "entry_rsi": c.get("entry_rsi"),
                "entry_vs_sma50": c.get("entry_vs_sma50"),
                "oversold": c.get("oversold"),
                "demote_reason": c.get("demote_reason"),
                "strategies": [s["name"] for s in c["strategies"]],
                "signal_date": c["latest_signal_date"] or as_of,
                "tracked_at": now,
                # Entry-time soft factors — the attribution ledger joins these with the realized
                # return once the idea closes (idea_ranking.factor_attribution). Neutral/None offline.
                "factors": {
                    "earnings": c.get("earnings_factor"), "news": c.get("news_factor"),
                    "sector": c.get("sector_factor"), "quality": c.get("quality_factor"),
                    "smart_money": c.get("smart_money_factor"),
                    "rank_multiplier": c.get("rank_multiplier"), "regime_fit": c.get("regime_fit"),
                },
            })
            open_keys.add(key)          # now open — don't re-add within this same pass
            added += 1
    # Commit the freshly-built curated basket as the new hysteresis anchor (once per run/track), and
    # tag its members' OPEN tracked records so the tracker/UI can tell the curated picks apart.
    basket_payload = ideas.get("basket") or {}
    _save_basket(idea_market, basket_payload)
    basket_alloc = {(it["ticker"], it["horizon"]): it.get("alloc_pct")
                    for it in basket_payload.get("items", [])}
    for t in tracked:
        if t.get("closed"):
            continue
        alloc = basket_alloc.get((t["ticker"], t.get("horizon")))
        if (t["ticker"], t.get("horizon")) in basket_alloc:
            t["source"] = "basket"
            t["basket_alloc_pct"] = alloc
        elif t.get("source") == "basket":     # fell out of the basket → no longer a curated pick
            t["source"] = None
            t.pop("basket_alloc_pct", None)

    _save_tracked(tracked)
    return {"added": added, "total": len(tracked),
            "basket": basket_payload.get("summary", {"count": 0})}


def _basket_portfolio_name(market: str) -> str:
    return f"Today's Basket — {(market or 'IN').upper()}"


async def _save_basket_portfolio(mk: str, pf_name: str, items: list, summary: dict,
                                 note: Optional[str], source: str) -> dict:
    """Turn a basket's items into a saved, NAV-tracked Portfolio (shared by the daily deploy and the hold
    book). Cost-basis-preserving: keeps the prior entry price on names still held. Returns the snapshot."""
    from pipeline_core.portfolio.models import (
        Portfolio, PortfolioBasis, PortfolioPosition, PortfolioSpec, WeightingScheme,
    )
    from pipeline_core.portfolio.track import snapshot_portfolio
    from pipeline_core.services import run_store

    existing = await run_store.get_portfolio(pf_name)
    prior_entry = {p.ticker: p.entry_price for p in existing.positions} if existing else {}
    positions = [
        PortfolioPosition(
            ticker=it["ticker"], sector=it.get("sector"),
            weight=round((it.get("alloc_pct") or it.get("weight_pct") or 0.0) / 100.0, 4)
            if (it.get("alloc_pct") is not None or it.get("weight_pct") is not None)
            else round(it.get("weight") or 0.0, 4),
            conviction=int(it.get("conviction") or 1),
            entry_price=prior_entry.get(it["ticker"], it.get("entry_price") or it.get("live_price") or it.get("price")),
            stop_pct=it.get("stop_pct"), stop_price=it.get("stop_price"),
            stop_source="basket", target_pct=it.get("target_pct"), score=it.get("basket_score"),
            rationale=(f"{it.get('horizon_label') or it.get('horizon') or ''} · conviction "
                       f"{it.get('conviction')} · exp {it.get('expected_return_pct')}% · win {it.get('win_prob')}%"),
        )
        for it in items
    ]
    spec = PortfolioSpec(name=pf_name, market=mk, basis=PortfolioBasis.SYSTEMATIC,
                         strategy_ids=["__basket__"], weighting=WeightingScheme.SCORE)
    pf = Portfolio(
        id=existing.id if existing else None, name=pf_name, spec=spec, positions=positions,
        cash_pct=round((summary.get("cash_pct") or 0.0) / 100.0, 4),
        summary={"source": source, "regime": summary.get("regime_state"),
                 "position_count": len(positions),
                 "invested_pct": round(sum(p.weight for p in positions), 4),
                 "expected_return_pct": summary.get("expected_return_pct"),
                 "avg_win_pct": summary.get("avg_win_pct"),
                 "deployed_at": datetime.now(timezone.utc).isoformat()},
        disclaimer=note,
    )
    saved = await run_store.save_portfolio(pf)
    snap = await snapshot_portfolio(saved)
    return {"portfolio_id": saved.id, "portfolio_name": pf_name, "snapshot": snap}


async def deploy_basket(market: str = "IN", name: Optional[str] = None,
                        max_per_bucket: int = 10) -> dict:
    """Materialize the daily curated basket as a saved, NAV-tracked **Portfolio** (tracked as a UNIT —
    NAV / P&L / rebalance / AI review). Idempotent + cost-basis-preserving. Returns
    {deployed, portfolio_id, snapshot, basket}."""
    mk = (market or "IN").upper()
    ideas = horizon_ideas(max_per_bucket, mk)
    if ideas is None:
        return {"deployed": False, "note": f"No {mk} backtest yet — run one first."}
    ideas = await enrich_ideas_live(ideas, mk)
    bk = ideas.get("basket") or {}
    items = bk.get("items", [])
    if not items:
        return {"deployed": False, "note": "The curated basket is empty right now — nothing to deploy."}
    summary = dict(bk.get("summary", {}), regime_state=bk.get("regime_state"))
    res = await _save_basket_portfolio(mk, name or _basket_portfolio_name(mk), items, summary,
                                       bk.get("note"), "basket")
    return {"deployed": True, **res, "basket": bk.get("summary", {})}


# ── My Hold Basket: commit → monitor for material change → apply ──

def _held_zone(item: dict, current: Optional[float]) -> Optional[str]:
    """Where a holding's LIVE price sits vs ITS OWN committed plan (not the board's re-anchored one)."""
    if current is None:
        return None
    sp, tp = item.get("stop_price"), item.get("target_price")
    if sp and current <= sp:
        return "below_stop"
    if tp and current >= tp:
        return "at_target"
    return "in_zone"


def _hold_item(it: dict, now: str, prior: Optional[dict]) -> dict:
    """A committed hold-basket item: the fresh pick + entry_price/committed_at (preserved for held names)."""
    return {
        "ticker": it["ticker"], "horizon": it.get("horizon"), "horizon_label": it.get("horizon_label"),
        "weight": round((it.get("alloc_pct") or 0.0) / 100.0, 4),
        "entry_price": (prior or {}).get("entry_price") or it.get("live_price") or it.get("price"),
        "committed_at": (prior or {}).get("committed_at") or now,
        "stop_price": it.get("stop_price"), "stop_pct": it.get("stop_pct"),
        "target_price": it.get("target_price"), "target_pct": it.get("target_pct"),
        "conviction": it.get("conviction"), "sector": it.get("sector"),
        "basket_score": it.get("basket_score"),
    }


async def _fresh_hold(market: str, max_per_bucket: int = 10):
    """(enriched board, fresh hold basket) for a market — the shared setup for commit / status / apply."""
    mk = (market or "IN").upper()
    ideas = horizon_ideas(max_per_bucket, mk)
    if ideas is None:
        return None, None
    ideas = await enrich_ideas_live(ideas, mk)
    return ideas, (ideas.get("hold_basket") or {})


async def commit_hold_basket(market: str = "IN", max_per_bucket: int = 10) -> dict:
    """Commit today's hold basket as the user's book: persist it (entry prices + committed_at, preserved
    for names carried from a prior commit) and deploy it as a NAV-tracked Portfolio. Returns the status."""
    mk = (market or "IN").upper()
    ideas, fresh = await _fresh_hold(mk, max_per_bucket)
    if ideas is None:
        return {"committed": False, "note": f"No {mk} backtest yet — run one first."}
    items = (fresh or {}).get("items", [])
    if not items:
        return {"committed": False, "note": (fresh or {}).get("note") or "No hold-worthy names right now."}
    now = datetime.now(timezone.utc).isoformat()
    prior = {it["ticker"]: it for it in (load_hold_basket(mk) or {}).get("items", [])}
    committed_items = [_hold_item(it, now, prior.get(it["ticker"])) for it in items]
    payload = {"market": mk, "committed_at": now, "size": fresh.get("size"),
               "items": committed_items, "note": fresh.get("note")}
    _save_hold_basket(mk, payload)
    await _save_basket_portfolio(mk, _hold_portfolio_name(mk), items,
                                 dict(fresh.get("summary", {}), regime_state=fresh.get("regime_state")),
                                 fresh.get("note"), "hold-basket")
    return await hold_basket_status(mk)   # return the freshly-committed status (no action needed)


def _mark_held(committed: dict, board_by: dict, price_of) -> list:
    """Assemble the `held` list the advisor needs: each committed item + live mark + board conviction."""
    held = []
    for it in committed.get("items", []):
        tk = it["ticker"]
        cur = price_of(tk)
        bc = board_by.get(tk)
        held.append({
            "ticker": tk, "weight": it.get("weight"), "horizon": it.get("horizon"),
            "entry_price": it.get("entry_price"), "current_price": cur,
            "stop_price": it.get("stop_price"), "target_price": it.get("target_price"),
            "committed_at": it.get("committed_at"),
            "zone": _held_zone(it, cur),                          # vs the holding's OWN committed plan
            "conviction": bc.get("conviction") if bc else None,  # current signal strength (None = off board)
        })
    return held


async def hold_basket_status(market: str = "IN", max_per_bucket: int = 10) -> dict:
    """The committed hold book's live state + the (usually empty) MATERIAL rebalance plan. Nothing is
    changed here — this is read-only advice the user reviews before applying."""
    mk = (market or "IN").upper()
    committed = load_hold_basket(mk)
    if not committed or not committed.get("items"):
        _, fresh = await _fresh_hold(mk, max_per_bucket)
        return {"committed": False, "market": mk, "preview": fresh or {"items": []}}

    ideas, fresh = await _fresh_hold(mk, max_per_bucket)
    board_by = {c["ticker"]: c for h in (ideas or {}).get("horizons", [])
                for c in ideas["buckets"][h]["candidates"]} if ideas else {}
    # Live marks for the holdings (concurrent, cached).
    tickers = [it["ticker"] for it in committed["items"]]
    sem = asyncio.Semaphore(_PRICE_FETCH_CONCURRENCY)

    async def _q(tk):
        async with sem:
            return tk, await asyncio.to_thread(_current_price, tk, mk)
    prices = dict(await asyncio.gather(*(_q(t) for t in tickers))) if tickers else {}

    held = _mark_held(committed, board_by, lambda tk: prices.get(tk))
    plan = basket.hold_rebalance_plan(held, (fresh or {}).get("items", []),
                                      target_size=(fresh or {}).get("size") or len(held))
    # Book-level marks.
    marked, tot_ret, invested = [], 0.0, 0.0
    for h in held:
        ret = basket._ret_pct(h["entry_price"], h["current_price"])
        w = h.get("weight") or 0.0
        if ret is not None:
            tot_ret += w * ret / 100.0
            invested += w
        marked.append({**h, "return_pct": ret})
    return {
        "committed": True, "market": mk,
        "committed_at": committed.get("committed_at"),
        "holdings": marked, "count": len(marked),
        "book_return_pct": round(tot_ret * 100, 2), "invested_pct": round(invested * 100, 1),
        "material": plan["material"], "actions": plan["actions"], "counts": plan["counts"],
        "target": plan["target"], "portfolio_name": _hold_portfolio_name(mk),
    }


async def apply_hold_rebalance(market: str = "IN", max_per_bucket: int = 10) -> dict:
    """Adopt the reviewed rebalance: resolve the plan's target (held − EXITs + material ADDs), keep the
    cost-basis on survivors, persist, and re-deploy the portfolio. Returns the new status."""
    mk = (market or "IN").upper()
    status = await hold_basket_status(mk, max_per_bucket)
    if not status.get("committed"):
        return {"applied": False, "note": "No committed hold basket to rebalance."}
    if not status.get("material"):
        return {"applied": False, "note": "No material changes — nothing to apply.", **status}

    _, fresh = await _fresh_hold(mk, max_per_bucket)
    fresh_by = {it["ticker"]: it for it in (fresh or {}).get("items", [])}
    prior = {it["ticker"]: it for it in (load_hold_basket(mk) or {}).get("items", [])}
    now = datetime.now(timezone.utc).isoformat()

    # Target = survivors + material adds. Fresh picks refresh their plan (keeping entry/committed if
    # carried); a survivor not in today's fresh ideal keeps its existing committed record as-is.
    new_items = []
    for tk in status["target"]:
        if tk in fresh_by:
            new_items.append(_hold_item(fresh_by[tk], now, prior.get(tk)))
        elif tk in prior:
            new_items.append(dict(prior[tk]))
    scale = 1.0 / (sum(i["weight"] for i in new_items) or 1.0) if sum(i["weight"] for i in new_items) > 1 else 1.0
    for i in new_items:
        i["weight"] = round(i["weight"] * scale, 4)

    _save_hold_basket(mk, {"market": mk, "committed_at": now, "size": (fresh or {}).get("size"),
                           "items": new_items, "note": (fresh or {}).get("note")})
    invested = sum(i["weight"] for i in new_items)
    await _save_basket_portfolio(mk, _hold_portfolio_name(mk), new_items,
                                 {"cash_pct": round((1 - invested) * 100, 1),
                                  "regime_state": (fresh or {}).get("regime_state")},
                                 (fresh or {}).get("note"), "hold-basket")
    return {"applied": True, **await hold_basket_status(mk, max_per_bucket)}


def uncommit_hold_basket(market: str = "IN") -> dict:
    """Forget the committed hold basket for a market (the NAV-tracked portfolio is left intact)."""
    _clear_hold_basket((market or "IN").upper())
    return {"uncommitted": True, "market": (market or "IN").upper()}


async def maybe_redeploy_basket(market: str = "IN") -> Optional[dict]:
    """Weekly re-sync of an ALREADY-deployed basket portfolio (the live 'rebalance'): if one exists and
    its last deploy was ≥ `basket_redeploy_days` ago, re-deploy it (idempotent, cost-basis-preserving).
    Never auto-CREATES one — deploying is an explicit user action. No-op when disabled / not yet due.
    Called from the background auto-refresh loop."""
    if not settings.basket_autodeploy_enabled:
        return None
    from pipeline_core.services import run_store

    existing = await run_store.get_portfolio(_basket_portfolio_name(market))
    if existing is None:
        return None
    last = (existing.summary or {}).get("deployed_at")
    if last:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            if age.days < max(1, settings.basket_redeploy_days):
                return None
        except (ValueError, TypeError):
            pass          # unparseable timestamp → treat as due
    return await deploy_basket(market)


def clear_tracked(market: Optional[str] = None) -> dict:
    """Forget tracked ideas (reset the scoreboard). Scoped to one market when given."""
    tracked = _load_tracked()
    if market is None:
        _save_tracked([])
        return {"cleared": len(tracked)}
    mk = market.upper()
    keep = [t for t in tracked if (t.get("market") or "IN") != mk]
    _save_tracked(keep)
    return {"cleared": len(tracked) - len(keep)}


def mark_idea_action(ticker: str, horizon: str, market: str = "IN",
                     action: str = "in", price: Optional[float] = None) -> dict:
    """Record what the USER actually did on a tracked idea — the 'you' side of the discipline scorecard
    (pipeline_core/adherence.py). action='in' stamps your entry, action='out' your exit, onto a
    `user_action` block of the most-recent OPEN tracked record for (ticker, horizon, market). Price
    defaults to the live quote. Marking 'out' without a prior 'in' adopts the plan's entry price."""
    mk = (market or "IN").upper()
    tk = ticker.upper()
    action = (action or "in").lower()
    if action not in ("in", "out"):
        return {"ok": False, "error": "action must be 'in' or 'out'"}
    tracked = _load_tracked()
    matches = [t for t in tracked if t.get("ticker") == tk and t.get("horizon") == horizon
               and (t.get("market") or "IN").upper() == mk and not t.get("closed")]
    if not matches:
        return {"ok": False, "error": f"no open tracked idea for {tk} ({horizon}, {mk})"}
    rec = max(matches, key=lambda t: t.get("tracked_at") or "")
    if price is None:
        price = _current_price(tk, mk)
    if price is None:
        return {"ok": False, "error": f"no live price for {tk}"}
    now = datetime.now(timezone.utc).isoformat()
    px = round_price(float(price))
    ua = rec.get("user_action") or {"entered": False, "exited": False}
    if action == "in":
        ua.update(entered=True, entry_price=px, entry_date=now)
    else:
        if not ua.get("entered"):                 # 'out' without 'in' → adopt the plan's entry
            ua.update(entered=True, entry_price=rec.get("entry_price"), entry_date=now)
        ua.update(exited=True, exit_price=px, exit_date=now)
    rec["user_action"] = ua
    _save_tracked(tracked)
    return {"ok": True, "ticker": tk, "horizon": horizon, "market": mk,
            "action": action, "price": px, "user_action": ua}


def _current_price(ticker: str, market: str = "IN") -> Optional[float]:
    """Freshest live price for mark-to-market (portfolio snapshot, rebalance, idea tracker).
    Delegates to the short-cached live quote so gains/losses reflect the latest price rather
    than a bar that could be up to 15 minutes stale."""
    return get_quote(ticker, market)


# Time-stop (trading days) per horizon — an idea that hasn't hit stop or target by then
# is closed at that day's price ("expired"). Long/trend ideas get a soft cap so the
# "past trades" concept applies to every horizon.
HORIZON_MAX_BARS = {"short": 15, "medium": 40, "long": 120}


def _roundtrip_cost_pct(idea: dict, bars_held: int) -> float:
    """Realistic round-trip trading cost as a percent of notional — the same the backtest engine
    prices in (config.market_cost_bps) plus, for a crypto SHORT, perpetual funding paid per day held.
    Returns 0.0 when ledger costs are off, so realized collapses to the raw gross move (prior behaviour)."""
    if not settings.ledger_costs_enabled:
        return 0.0
    from pipeline_core.config import market_cost_bps
    market = idea.get("market") or "IN"
    cost = market_cost_bps(market) / 100.0                       # bps → percent, round trip
    if str(market).upper() == "CRYPTO" and idea.get("side") == "short":
        cost += settings.crypto_funding_bps_per_day * max(0, bars_held or 0) / 100.0
    return cost


def _closed_outcome(idea: dict, reason: str, exit_price: float, exit_date: str, bars_held: int) -> dict:
    """The lifecycle fields written onto a tracked idea when it closes. A SHORT realizes the inverse
    price move (it profits when the exit is below entry). `realized_return_pct` is NET of realistic
    round-trip cost (config.market_cost_bps + crypto-short funding); the raw move is kept as
    `gross_return_pct` for transparency. See settings.ledger_costs_enabled."""
    entry = idea.get("entry_price")
    sign = -1 if idea.get("side") == "short" else 1
    gross = round(sign * (exit_price - entry) / entry * 100, 2) if entry else None
    cost = _roundtrip_cost_pct(idea, bars_held)
    realized = round(gross - cost, 2) if gross is not None else None
    return {
        "closed": True,
        "exit_reason": reason,                 # "stop" | "target" | "time"
        "exit_price": round_price(exit_price),
        "exit_date": exit_date,
        "realized_return_pct": realized,       # NET of round-trip cost
        "gross_return_pct": gross,             # raw price move, pre-cost
        "cost_pct": round(cost, 3),
        "bars_held": bars_held,
        "_cost_adjusted": True,
    }


def _effective_target(idea: dict) -> Optional[float]:
    """The profit target to close a trade at — the stored one, or a fallback derived
    from `target_pct`/the horizon default. Legacy ideas were snapshotted without a
    `target_price`, so without this they could only ever close on stop or time and a
    completed winner would linger forever in the open list."""
    t = idea.get("target_price")
    if t is not None:
        return t
    entry = idea.get("entry_price")
    if not entry:
        return None
    pct = idea.get("target_pct")
    if pct is None:
        pct = HORIZON_DEFAULT_TARGET.get(idea.get("horizon", "medium"), 0.18)
    sign = -1 if idea.get("side") == "short" else 1          # short target is below entry
    return round_price(entry * (1 + sign * pct))


def _evaluate_idea_path(idea: dict, df) -> Optional[dict]:
    """Walk daily OHLC forward from the signal day and decide whether the idea has
    closed (hit stop, hit target, or expired). Returns the closed-outcome dict, or None
    if it's still open / can't be evaluated (caller then live-marks it)."""
    entry = idea.get("entry_price")
    stop = idea.get("stop_price")
    target = _effective_target(idea)
    horizon = idea.get("horizon", "medium")
    sig = idea.get("signal_date") or (idea.get("tracked_at") or "")[:10]
    if df is None or getattr(df, "empty", True) or not entry or not sig:
        return None

    d = df.copy()
    if d.index.tz is not None:                 # normalize tz-aware (yfinance) to naive
        d.index = d.index.tz_localize(None)
    try:
        sub = d[d.index >= pd.Timestamp(sig)]
    except Exception:
        sub = d
    bars = sub.iloc[1:]                         # skip the entry (signal-day) bar
    max_bars = HORIZON_MAX_BARS.get(horizon)
    is_short = idea.get("side") == "short"      # short: stop above entry, target below

    for i, (ts, row) in enumerate(bars.iterrows(), start=1):
        low, high, close = float(row["Low"]), float(row["High"]), float(row["Close"])
        exit_date = str(ts.date())
        if is_short:
            if stop is not None and high >= stop:       # stop first when both touch a bar
                return _closed_outcome(idea, "stop", stop, exit_date, i)
            if target is not None and low <= target:
                return _closed_outcome(idea, "target", target, exit_date, i)
        else:
            if stop is not None and low <= stop:        # stop first when both touch a bar
                return _closed_outcome(idea, "stop", stop, exit_date, i)
            if target is not None and high >= target:
                return _closed_outcome(idea, "target", target, exit_date, i)
        if max_bars is not None and i >= max_bars:
            return _closed_outcome(idea, "time", close, exit_date, i)
    return None                                # still open


def _open_scoreboard(ideas: list) -> dict:
    """Live (unrealized) tally for still-open ideas."""
    winning = sum(1 for i in ideas if i["status"] == "winning")
    losing = sum(1 for i in ideas if i["status"] == "losing")
    flat = sum(1 for i in ideas if i["status"] == "flat")
    unknown = sum(1 for i in ideas if i["status"] == "unknown")
    decided = winning + losing
    rets = [i["return_pct"] for i in ideas if i["return_pct"] is not None]
    return {
        "open": len(ideas), "winning": winning, "losing": losing, "flat": flat, "unknown": unknown,
        "win_rate": round(winning / decided * 100, 1) if decided else None,
        "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
    }


def _compound_return_pct(rets: list) -> Optional[float]:
    """Compounded return of taking every trade equal-weight, as a percent. Compounding (not summing
    the raw percentages) is the honest 'book' figure: it can't fall below −100% the way `sum(rets)`
    can, so 43 trades averaging −2.4% read as a real drawdown, not an impossible >100% wipeout."""
    if not rets:
        return None
    equity = 1.0
    for r in rets:
        equity *= 1.0 + r / 100.0
    return round((equity - 1.0) * 100.0, 2)


def _realized_scoreboard(closed: list) -> dict:
    """Realized track record from closed trades — the credible win/loss record."""
    rets = [c["realized_return_pct"] for c in closed if c.get("realized_return_pct") is not None]
    wins = sum(1 for r in rets if r > 0)
    losses = sum(1 for r in rets if r < 0)
    decided = wins + losses
    by_reason: dict[str, int] = {}
    for c in closed:
        by_reason[c.get("exit_reason", "?")] = by_reason.get(c.get("exit_reason", "?"), 0) + 1
    return {
        "closed": len(closed), "wins": wins, "losses": losses,
        "targets_hit": by_reason.get("target", 0), "stopped": by_reason.get("stop", 0),
        "expired": by_reason.get("time", 0),
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "median_return_pct": round(statistics.median(rets), 2) if rets else None,
        # Compounded equal-weight book return — bounded, unlike the old sum(rets) that could print <−100%.
        "total_return_pct": _compound_return_pct(rets),
    }


def _idea_equity_curve(closed: list) -> list:
    """Compounded realized P&L over exit dates — an equal-weight 'took every idea' book. Compounds
    each closed trade (`equity *= 1+r`) rather than summing raw percentages, so the curve is a real
    reinvested-bankroll track that floors near −100% instead of running arbitrarily negative."""
    pts = sorted(
        (c for c in closed if c.get("exit_date") and c.get("realized_return_pct") is not None),
        key=lambda c: c["exit_date"],
    )
    equity, out = 1.0, []
    for c in pts:
        equity *= 1.0 + c["realized_return_pct"] / 100.0
        try:
            ts = int(pd.Timestamp(c["exit_date"]).timestamp() * 1000)
        except Exception:
            continue
        out.append([ts, round((equity - 1.0) * 100.0, 2)])   # cumulative % return of the book
    return out


def _period_for_days(days: int) -> str:
    """Smallest yfinance/Binance period covering `days` of history since entry."""
    if days <= 25:
        return "1mo"
    if days <= 80:
        return "3mo"
    if days <= 175:
        return "6mo"
    if days <= 350:
        return "1y"
    return "2y"


def _idea_age_days(idea: dict) -> int:
    sig = idea.get("signal_date") or (idea.get("tracked_at") or "")[:10]
    try:
        d = datetime.fromisoformat(sig).replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - d).days)
    except Exception:
        return 200


async def tracked_ideas_status(market: Optional[str] = None) -> dict:
    """Lifecycle status of tracked ideas. Open ideas are evaluated forward from their
    signal day: any that hit their stop, target, or time-stop are **closed** (recorded
    with a realized return and moved to past trades — they never flip back). Still-open
    ideas are live-marked. Prices/history are fetched live (cached). Scoped to one asset
    class when `market` is given."""
    all_tracked = _load_tracked()
    mk = market.upper() if market else None
    subset = [t for t in all_tracked if mk is None or (t.get("market") or "IN") == mk]
    if not subset:
        return {"ideas": [], "open": [], "closed": [], "count": 0, "priced_at": None,
                "scoreboard": _open_scoreboard([]), "closed_scoreboard": _realized_scoreboard([]),
                "attribution": idea_ranking.factor_attribution([]),
                "rank_efficiency": idea_ranking.rank_efficiency([]), "equity_curve": []}

    open_before = [t for t in subset if not t.get("closed")]

    # Load daily history since entry, once per (ticker, market), for open ideas.
    need: dict[tuple, int] = {}
    for t in open_before:
        key = (t["ticker"], (t.get("market") or "IN"))
        need[key] = max(need.get(key, 0), _idea_age_days(t))

    sem = asyncio.Semaphore(_PRICE_FETCH_CONCURRENCY)

    async def fetch_hist(item):
        (tk, mkt), days = item
        async with sem:
            df = await asyncio.to_thread(get_price_history, tk, _period_for_days(days + 10), "1d", mkt)
            return (tk, mkt), df

    histories = dict(await asyncio.gather(*(fetch_hist(i) for i in need.items())))

    # Live mark uses a FRESH quote (bypasses the 15-min history cache) so open gains/losses
    # reflect the latest price. Path-eval below still runs on the cached history — past daily
    # bars are immutable, so closures don't need a re-fetch.
    async def fetch_quote(key):
        tk, mkt = key
        async with sem:
            return key, await asyncio.to_thread(_current_price, tk, mkt)

    quotes = dict(await asyncio.gather(*(fetch_quote(k) for k in need))) if need else {}

    # Sell-side EXIT signal: has the smart money turned AGAINST an open position since entry — the pros now
    # distributing a long you hold (or accumulating a short you hold)? Read-only nudge; never auto-closes.
    sm_signals: dict = {}
    if settings.smart_money_distribution_enabled and need:
        async def fetch_sm(key):
            tk, mkt = key
            async with sem:
                return key, await asyncio.to_thread(smart_money.get_signal, mkt, tk)
        sm_signals = dict(await asyncio.gather(*(fetch_sm(k) for k in need)))

    now = datetime.now(timezone.utc).isoformat()

    closed_display, open_display, changed = [], [], False
    for t in subset:
        if t.get("closed"):
            closed_display.append(t)
            continue
        key = (t["ticker"], (t.get("market") or "IN"))
        df = histories.get(key)
        outcome = _evaluate_idea_path(t, df)
        if outcome:                                     # newly closed → persist once
            t.update(outcome)
            t["closed_at"] = now
            changed = True
            closed_display.append(t)
        else:                                           # still open → live mark
            cur = quotes.get(key)                        # fresh quote…
            if cur is None and df is not None and len(df):
                cur = round_price(float(df["Close"].iloc[-1]))   # …fallback to last cached bar
            entry = t.get("entry_price")
            ret, status = None, "unknown"
            if cur is not None and entry:
                sign = -1 if t.get("side") == "short" else 1     # a short gains when price falls
                ret = round(sign * (cur - entry) / entry * 100, 2)
                status = "winning" if ret > 0 else "losing" if ret < 0 else "flat"
            # "Normal drawdown" check: how the live drawdown sits vs the winners' typical/deep dip band
            # captured at entry — reassurance while within it, a warning past it. None when not down / no band.
            typ, deep = t.get("typical_drawdown_pct"), t.get("deep_drawdown_pct")
            dd_status, dd_pct = None, None
            if ret is not None and ret < 0 and typ is not None:
                dd_pct = round(-ret, 2)
                dd_status = ("within" if dd_pct <= typ
                             else "stretching" if (deep is None or dd_pct <= deep)
                             else "beyond")
            exit_flag = (idea_ranking.smart_money_exit_flag(sm_signals.get(key), t.get("side", "long"))
                         if settings.smart_money_distribution_enabled else None)
            open_display.append({**t, "current_price": cur, "return_pct": ret, "status": status,
                                 "dd_status": dd_status, "dd_pct": dd_pct,
                                 "smart_money_exit": exit_flag})

    if changed:
        _save_tracked(all_tracked)                      # closures mutated in place

    open_display.sort(key=lambda x: (x["return_pct"] is not None, x["return_pct"] or 0), reverse=True)
    closed_display.sort(key=lambda c: c.get("exit_date") or "", reverse=True)
    return {
        "ideas": open_display,                          # back-compat: the live/open list
        "open": open_display,
        "closed": closed_display,
        "count": len(subset),
        "scoreboard": _open_scoreboard(open_display),
        "closed_scoreboard": _realized_scoreboard(closed_display),
        "attribution": idea_ranking.factor_attribution(closed_display),   # realized lift per factor
        "rank_efficiency": idea_ranking.rank_efficiency(closed_display),   # does rank predict return? (north-star)
        "equity_curve": _idea_equity_curve(closed_display),
        "priced_at": now,
    }
