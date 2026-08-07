"""
Intraday bot backtest harness — a **triple-barrier walk-forward replay** that measures each
signal bot's real edge.

The daily Strategy-Lab engine (`backtest/engine.py`) is signal-*series* based; the intraday
bots are point-in-time `evaluate(df, ctx)` calls, so they need a different shape: replay the bot
bar-by-bar over deep history, and every time it fires, open a trade with the bot's own
entry/stop/target and follow the price forward until it hits the **target** (win), the **stop**
(loss), or a **time** limit — the classic triple-barrier label. Aggregate the outcomes into
per-bot × timeframe × regime win-rate / expectancy / profit-factor so idea conviction can be
weighted by measured edge instead of a raw vote count.

No look-ahead: signals are read on **closed** bars and entered at the *next* bar's open; the
higher-timeframe / BTC context a bot sees at bar *i* uses only HTF bars that fully closed by *i*.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from pipeline_core.agents import indicators as ind
from pipeline_core.bots.base import get_bot, htf_for, list_bots
from pipeline_core.config import DATA_DIR, settings
from pipeline_core.markets import get_universe_tickers
from pipeline_core.services import crypto_data

logger = logging.getLogger(__name__)

INTRADAY_DIR = DATA_DIR / "backtests" / "intraday"
INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
EDGE_FILE = INTRADAY_DIR / "edge.json"

# Replay tuning
LOOKBACK = 150          # bounded window handed to evaluate() → each call is O(LOOKBACK), not O(i)
WARMUP = 60             # first bar we start scanning from (indicator warm-up)
COST_BPS = 20.0         # round-trip cost: taker fee (~7bps/side) + est half-spread; funding on
                        # short intraday holds is small and folded in here
TREND_ADX = 25.0        # ADX above this = trending regime, else ranging
MIN_SAMPLE = 20         # fewer trades than this → stats shown but flagged not-enough
# Time barrier (max bars held) per timeframe — the intraday horizon before a trade is force-exited.
MAX_BARS = {"5m": 48, "10m": 36, "15m": 32, "30m": 32, "1h": 24, "4h": 18, "1d": 10}

_HTF_RULE = {"1h": "1h", "4h": "4h", "1d": "1D"}
_HTF_DELTA = {"1h": pd.Timedelta("1h"), "4h": pd.Timedelta("4h"), "1d": pd.Timedelta("1D")}

# In-memory job state (single intraday backtest at a time, independent of the daily runner's).
intraday_job_state: dict = {
    "running": False, "stage": None, "progress_pct": 0.0, "detail": "",
    "started_at": None, "finished_at": None, "error": None,
}


def _set_job(stage: str, pct: float, detail: str = "") -> None:
    intraday_job_state.update(stage=stage, progress_pct=round(pct, 1), detail=detail)


# ── Triple-barrier replay ───────────────────────────────────────────


def _resample_htf(df: pd.DataFrame, htf: str) -> pd.DataFrame:
    rule = _HTF_RULE.get(htf, "1h")
    return df.resample(rule).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Close"])


def replay_bot(bot, df: pd.DataFrame, tf: str, adx_full: np.ndarray,
               htf_full: Optional[pd.DataFrame], htf_end: Optional[np.ndarray],
               btc_full: Optional[pd.DataFrame], btc_end: Optional[np.ndarray],
               cost_frac: float) -> list[dict]:
    """Walk `bot` forward over one coin's history; return one dict per closed trade."""
    n = len(df)
    if n < WARMUP + 5:
        return []
    opens = df["Open"].to_numpy(float)
    highs = df["High"].to_numpy(float)
    lows = df["Low"].to_numpy(float)
    closes = df["Close"].to_numpy(float)
    idx = df.index
    max_bars = MAX_BARS.get(tf, 24)
    trades: list[dict] = []

    i = WARMUP
    while i < n - 1:
        window = df.iloc[max(0, i - LOOKBACK): i + 1]
        ctx = {"timeframe": tf, "htf_df": None, "btc_df": None}
        t64 = idx[i].to_datetime64()
        if bot.needs_htf and htf_end is not None:
            k = int(np.searchsorted(htf_end, t64, side="right"))   # HTF bars fully closed by i
            ctx["htf_df"] = htf_full.iloc[max(0, k - 80): k]
        if bot.needs_btc and btc_end is not None:
            kb = int(np.searchsorted(btc_end, t64, side="right"))
            ctx["btc_df"] = btc_full.iloc[max(0, kb - 80): kb]
        try:
            sig = bot.evaluate(window, ctx)
        except Exception:
            i += 1
            continue

        side = sig.get("side")
        if side not in ("long", "short") or not (sig["entry"] and sig["stop"] and sig["target"]):
            i += 1
            continue

        entry_i = i + 1
        entry_px = opens[entry_i]
        if not np.isfinite(entry_px) or entry_px <= 0:
            i += 1
            continue
        stop, target = sig["stop"], sig["target"]
        s = 1.0 if side == "long" else -1.0

        exit_i, exit_px, reason = None, None, "time"
        last = min(entry_i + max_bars, n - 1)
        for j in range(entry_i, last + 1):
            hi, lo = highs[j], lows[j]
            if not (np.isfinite(hi) and np.isfinite(lo)):
                continue
            if side == "long":
                if lo <= stop:                       # stop checked first (conservative)
                    exit_i, exit_px, reason = j, stop, "stop"; break
                if hi >= target:
                    exit_i, exit_px, reason = j, target, "target"; break
            else:
                if hi >= stop:
                    exit_i, exit_px, reason = j, stop, "stop"; break
                if lo <= target:
                    exit_i, exit_px, reason = j, target, "target"; break
        if exit_i is None:                            # time barrier → exit at last close
            exit_i, exit_px, reason = last, closes[last], "time"

        gross = s * (exit_px / entry_px - 1.0)
        net = gross - cost_frac
        adxv = adx_full[i] if i < len(adx_full) else np.nan
        regime = "trend" if (np.isfinite(adxv) and adxv > TREND_ADX) else "range"
        trades.append({
            "side": side, "reason": reason, "ret_pct": round(net * 100, 3),
            "win": net > 0, "bars_held": exit_i - entry_i, "regime": regime,
            "planned_r": sig.get("r_multiple"), "entry_px": entry_px,
            "entry_date": str(idx[entry_i]),
        })
        i = exit_i + 1                                # no overlapping trades for this bot/coin
    return trades


# ── Aggregation ─────────────────────────────────────────────────────


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
                "expectancy_pct": 0.0, "profit_factor": 0.0, "avg_bars_held": 0.0,
                "target_exits": 0, "stop_exits": 0, "time_exits": 0, "enough": False}
    rets = [t["ret_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "avg_win_pct": round(float(np.mean(wins)), 3) if wins else 0.0,
        "avg_loss_pct": round(float(np.mean(losses)), 3) if losses else 0.0,
        "expectancy_pct": round(float(np.mean(rets)), 3),
        "profit_factor": round(pf, 2) if np.isfinite(pf) else "inf",
        "avg_bars_held": round(float(np.mean([t["bars_held"] for t in trades])), 1),
        "target_exits": sum(1 for t in trades if t["reason"] == "target"),
        "stop_exits": sum(1 for t in trades if t["reason"] == "stop"),
        "time_exits": sum(1 for t in trades if t["reason"] == "time"),
        "enough": n >= MIN_SAMPLE,
    }


def _bundle(trades: list[dict]) -> dict:
    b = _stats(trades)
    b["by_regime"] = {r: _stats([t for t in trades if t["regime"] == r]) for r in ("trend", "range")}
    b["by_side"] = {sd: _stats([t for t in trades if t["side"] == sd]) for sd in ("long", "short")}
    # Joint regime×side cells: a bot's edge is often side-dependent (e.g. positive short, negative
    # long) so the idea engine must attribute by the *specific* side it fired, not the regime alone.
    b["by_regime_side"] = {
        r: {sd: _stats([t for t in trades if t["regime"] == r and t["side"] == sd])
            for sd in ("long", "short")}
        for r in ("trend", "range")
    }
    return b


# ── Data loading ────────────────────────────────────────────────────


async def _load_deep(symbols: list[str], tf: str, bars: int, progress=None) -> dict[str, pd.DataFrame]:
    sem = asyncio.Semaphore(settings.ingestion_concurrency)
    out: dict[str, pd.DataFrame] = {}
    done = 0

    async def one(sym: str):
        nonlocal done
        async with sem:
            try:
                df = await asyncio.to_thread(crypto_data.get_klines, sym, tf, bars, True)
                if df is not None and len(df) >= WARMUP + 50:
                    out[sym] = df
            except Exception as e:
                logger.warning(f"intraday backtest load failed for {sym} {tf}: {e}")
            finally:
                done += 1
                if progress:
                    progress(done, len(symbols))

    await asyncio.gather(*(one(s) for s in symbols))
    return out


# ── Orchestration ───────────────────────────────────────────────────


async def run_intraday_backtest(
    bot_ids: Optional[list[str]] = None,
    timeframes: tuple[str, ...] = ("15m", "1h", "4h"),
    universe: str = "Top 20",
    bars: int = 1500,
) -> dict:
    """Replay the signal bots over deep intraday history and persist the edge table.

    Defaults span 15m/1h/4h × Top 20 × 1500 bars (≈ a few minutes) so a plain refresh measures the
    higher timeframes where the tradable edge actually lives, not just the noisy 15m. Narrow the
    `timeframes`/`universe` for a faster, shallower run. Runs as a single background job."""
    if intraday_job_state["running"]:
        return {"error": "An intraday backtest is already running"}
    intraday_job_state.update(running=True, error=None, started_at=datetime.now(timezone.utc).isoformat(),
                              finished_at=None)
    _set_job("starting", 0.0, "")
    cost_frac = COST_BPS / 10_000.0
    try:
        bots = [b for b in (get_bot(bid) for bid in
                            (bot_ids or [d["id"] for d in list_bots() if d["kind"] == "signal"]))
                if b and b.kind == "signal"]
        symbols = await asyncio.to_thread(get_universe_tickers, "CRYPTO", universe)
        edge: dict[str, dict] = {}
        coverage = {"coins": 0, "timeframes": list(timeframes), "bars": bars, "universe": universe}

        for ti, tf in enumerate(timeframes):
            base = ti / len(timeframes) * 100
            span = 100 / len(timeframes)
            _set_job(f"loading {tf}", base, f"{len(symbols)} coins")
            frames = await _load_deep(
                symbols, tf, bars,
                progress=lambda d, t: _set_job(f"loading {tf}", base + span * 0.4 * d / max(t, 1), f"{d}/{t} coins"))
            if not frames:
                continue
            coverage["coins"] = max(coverage["coins"], len(frames))

            # BTC higher-timeframe context (once per timeframe).
            btc_tf = frames.get("BTC")
            if btc_tf is None:
                btc_tf = await asyncio.to_thread(crypto_data.get_klines, "BTC", tf, bars, True)
            htf = htf_for(tf)
            btc_htf = _resample_htf(btc_tf, htf) if btc_tf is not None and not btc_tf.empty else None
            btc_end = (btc_htf.index + _HTF_DELTA.get(htf, pd.Timedelta("1h"))).values if btc_htf is not None else None

            trades_by_bot: dict[str, list] = defaultdict(list)
            items = list(frames.items())
            for ci, (sym, df) in enumerate(items):
                htf_full = _resample_htf(df, htf)
                htf_end = (htf_full.index + _HTF_DELTA.get(htf, pd.Timedelta("1h"))).values
                adx_full = ind.adx(df["High"], df["Low"], df["Close"], 14)["adx"].to_numpy(float)
                for bot in bots:
                    try:
                        trades_by_bot[bot.name] += replay_bot(
                            bot, df, tf, adx_full, htf_full, htf_end, btc_htf, btc_end, cost_frac)
                    except Exception as e:
                        logger.warning(f"replay failed {bot.id}/{sym}/{tf}: {e}")
                _set_job(f"replaying {tf}", base + span * (0.4 + 0.6 * (ci + 1) / len(items)),
                         f"{ci + 1}/{len(items)} coins")

            for bot in bots:
                edge[f"{bot.name}|{tf}"] = _bundle(trades_by_bot[bot.name])

        payload = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "params": {"cost_bps": COST_BPS, "trend_adx": TREND_ADX, "max_bars": MAX_BARS,
                       "min_sample": MIN_SAMPLE},
            "coverage": coverage,
            "bots": edge,
        }
        _persist_edge(payload)
        _set_job("done", 100.0, f"{sum(v['trades'] for v in edge.values())} trades")
        return {"status": "completed", "keys": len(edge),
                "total_trades": sum(v["trades"] for v in edge.values()), "coverage": coverage}
    except Exception as e:
        intraday_job_state["error"] = str(e)
        logger.exception("intraday backtest failed")
        return {"error": str(e)}
    finally:
        intraday_job_state.update(running=False, finished_at=datetime.now(timezone.utc).isoformat())


# ── Persistence + lookup ────────────────────────────────────────────


def _persist_edge(payload: dict) -> None:
    tmp = EDGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(EDGE_FILE)


def load_edge() -> Optional[dict]:
    if not EDGE_FILE.exists():
        return None
    try:
        return json.loads(EDGE_FILE.read_text())
    except Exception as e:
        logger.warning(f"could not read intraday edge file: {e}")
        return None


def edge_for(bot_name: str, tf: str, regime: Optional[str] = None, side: Optional[str] = None,
             edge: Optional[dict] = None) -> Optional[dict]:
    """Stats for one bot on a timeframe, or None if absent. Returns the **most specific cell with a
    reliable sample** (≥ MIN_SAMPLE), preferring: joint regime×side → side → regime → overall. A
    caller that passes only `regime` (side=None) gets exactly the previous regime-or-overall
    behaviour, so existing call sites are unchanged."""
    data = edge if edge is not None else load_edge()
    if not data:
        return None
    entry = (data.get("bots") or {}).get(f"{bot_name}|{tf}")
    if not entry:
        return None
    if regime and side:
        cell = entry.get("by_regime_side", {}).get(regime, {}).get(side)
        if cell and cell.get("trades", 0) >= MIN_SAMPLE:
            return cell
    if side:
        cell = entry.get("by_side", {}).get(side)
        if cell and cell.get("trades", 0) >= MIN_SAMPLE:
            return cell
    if regime and entry.get("by_regime", {}).get(regime, {}).get("trades", 0) >= MIN_SAMPLE:
        return entry["by_regime"][regime]
    return entry


def status() -> dict:
    """Job state + a compact summary of the last completed run."""
    edge = load_edge()
    summary = None
    if edge:
        summary = {"as_of": edge.get("as_of"), "coverage": edge.get("coverage"),
                   "keys": len(edge.get("bots", {})),
                   "total_trades": sum(v.get("trades", 0) for v in edge.get("bots", {}).values())}
    return {"job": dict(intraday_job_state), "last_run": summary}
