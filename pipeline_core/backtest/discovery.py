"""
Automated strategy **discovery** — generate candidate strategies, backtest them, and keep only
the ones whose edge survives out-of-sample.

The Strategy Lab's 35 strategies are hand-written. This module *invents* new ones from a
parametric grammar over the existing indicator library, then subjects each to the only test that
separates a real edge from a curve-fit: **out-of-sample validation**.

  1. GENERATE — random-search a grammar of entry rules (trend / mean-reversion / breakout
     families, each with a small discrete param grid) × a risk overlay (stop / target / trail /
     time). Random search is deliberate: a greedy/genetic optimiser on one dataset overfits
     *harder*; random sampling + an honest OOS gate is the robust baseline.
  2. VALIDATE — split history at `split` (default 70%). Rank every candidate on the **in-sample**
     slice, take the top-K, then re-test those on the **held-out out-of-sample** slice they were
     never selected on. The engine's new `entry_start`/`entry_end` window gates *entries* by date
     while warm-up still uses the full history (no look-ahead, no truncation).
  3. KEEP — a survivor must clear a multi-criteria gate on BOTH slices (trade-count floor, positive
     expectancy, Sharpe, profit-factor) AND a **degradation guard** (OOS Sharpe ≥ ratio × IS
     Sharpe): an in-sample-only winner is rejected. `tried` vs `kept` is recorded for
     multiple-testing honesty.

Survivors are persisted to `data/backtests/discovered_<MARKET>.json` as a **staging catalog**;
nothing enters the live library or Trade Ideas until `promote_discovered` marks it, after which
the runner compiles its spec back into a StrategyDef and treats it like any built-in.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from pipeline_core.agents import indicators as ind
from pipeline_core.backtest.data_loader import load_benchmark, load_universe_history
from pipeline_core.backtest.engine import backtest_strategy
from pipeline_core.backtest.strategies import (
    StrategyDef,
    _crosses_above,
    _crosses_below,
    _fill,
    _never,
)
from pipeline_core.config import DATA_DIR
from pipeline_core.services.market_data import get_index_tickers
from pipeline_core.utils import deep_sanitize

logger = logging.getLogger(__name__)

DISCOVERED_DIR = DATA_DIR / "backtests"
DISCOVERED_DIR.mkdir(parents=True, exist_ok=True)

# ── "Promising" gate (module defaults; overridable per run) ─────────
MIN_TRADES_IS = 40          # statistical-significance floor, in-sample
MIN_TRADES_OOS = 15         # …and out-of-sample
MIN_SHARPE = 0.5            # required on both slices
MIN_PROFIT_FACTOR = 1.2    # required on both slices
DEGRADE_RATIO = 0.5        # OOS Sharpe must be ≥ this × IS Sharpe (anti-overfit core)
DEFAULT_SPLIT = 0.70       # fraction of the date span used for in-sample selection
DEFAULT_N = 200            # candidates sampled
DEFAULT_TOP_K = 25         # in-sample leaders re-tested out-of-sample
COST_BPS = 25.0

SignalFn = Callable[[pd.DataFrame], pd.Series]


# ── Grammar: entry-rule families ────────────────────────────────────
# Each family declares a discrete param grid, an entry builder, a *paired* exit builder (or None →
# a pure risk overlay), a category, and a warm-up. build_strategy() compiles a sampled spec into a
# StrategyDef, reusing the indicator library + the shared signal helpers from strategies.py.


def _ma_cross_entry(p):
    return lambda df: _fill(_crosses_above(ind.sma(df["Close"], p["fast"]), ind.sma(df["Close"], p["slow"])))


def _ma_cross_exit(p):
    return lambda df: _fill(_crosses_below(ind.sma(df["Close"], p["fast"]), ind.sma(df["Close"], p["slow"])))


def _dma_reclaim_entry(p):
    def f(df):
        c = df["Close"]
        dma = ind.sma(c, p["n"])
        return _fill(_crosses_above(c, dma) & (dma.diff(5) > 0))
    return f


def _dma_reclaim_exit(p):
    return lambda df: _fill(df["Close"] < ind.sma(df["Close"], p["n"]))


def _macd_cross_entry(p):
    def f(df):
        m = ind.macd(df["Close"])
        sig = _crosses_above(m["macd"], m["signal"])
        if p["trend_filter"]:
            sig = sig & (df["Close"] > ind.sma(df["Close"], 200))
        return _fill(sig)
    return f


def _macd_cross_exit(_p):
    return lambda df: _fill(_crosses_below(ind.macd(df["Close"])["macd"], ind.macd(df["Close"])["signal"]))


def _roc_entry(p):
    def f(df):
        r = ind.roc(df["Close"], p["n"])
        return _fill(_crosses_above(r, pd.Series(float(p["th"]), index=df.index)) & (df["Close"] > ind.sma(df["Close"], 200)))
    return f


def _roc_exit(p):
    return lambda df: _fill(ind.roc(df["Close"], p["n"]) < 0)


def _supertrend_entry(p):
    def f(df):
        st = ind.supertrend(df["High"], df["Low"], df["Close"], p["period"], float(p["mult"]))
        up = st["uptrend"].astype(bool)
        return _fill(up & ~up.shift(1).fillna(False).astype(bool))
    return f


def _supertrend_exit(p):
    def f(df):
        st = ind.supertrend(df["High"], df["Low"], df["Close"], p["period"], float(p["mult"]))
        up = st["uptrend"].astype(bool)
        return _fill(~up & up.shift(1).fillna(False).astype(bool))
    return f


def _adx_entry(p):
    def f(df):
        a = ind.adx(df["High"], df["Low"], df["Close"], 14)
        return _fill(_crosses_above(a["plus_di"], a["minus_di"]) & (a["adx"] > p["th"]))
    return f


def _adx_exit(_p):
    def f(df):
        a = ind.adx(df["High"], df["Low"], df["Close"], 14)
        return _fill(_crosses_below(a["plus_di"], a["minus_di"]))
    return f


def _rsi_entry(p):
    def f(df):
        r = ind.rsi(df["Close"], p["n"])
        return _fill(_crosses_above(r, pd.Series(float(p["th"]), index=df.index)))
    return f


def _rsi_exit(p):
    return lambda df: _fill(ind.rsi(df["Close"], p["n"]) > p["exit_th"])


def _bb_reversion_entry(p):
    def f(df):
        bb = ind.bollinger_bands(df["Close"], 20, float(p["std"]))
        return _fill(_crosses_above(df["Close"], bb["lower"]))
    return f


def _bb_reversion_exit(_p):
    def f(df):
        bb = ind.bollinger_bands(df["Close"], 20, 2.0)
        return _fill(df["Close"] >= bb["mid"])
    return f


def _williams_entry(_p):
    def f(df):
        w = ind.williams_r(df["High"], df["Low"], df["Close"], 14)
        return _fill(_crosses_above(w, pd.Series(-80.0, index=df.index)))
    return f


def _williams_exit(_p):
    def f(df):
        w = ind.williams_r(df["High"], df["Low"], df["Close"], 14)
        return _fill(w > -30)
    return f


def _cci_entry(_p):
    def f(df):
        c = ind.cci(df["High"], df["Low"], df["Close"], 20)
        return _fill(_crosses_above(c, pd.Series(-100.0, index=df.index)))
    return f


def _cci_exit(_p):
    def f(df):
        c = ind.cci(df["High"], df["Low"], df["Close"], 20)
        return _fill(c > 100)
    return f


def _donchian_entry(p):
    def f(df):
        hi = ind.rolling_high(df["High"], p["n"])
        return _fill(df["Close"] > hi)
    return f


def _keltner_entry(_p):
    def f(df):
        k = ind.keltner_channels(df["High"], df["Low"], df["Close"], 20, 20, 2.0)
        return _fill((df["Close"] > k["upper"]) & (df["Close"] > ind.sma(df["Close"], 200)))
    return f


def _keltner_exit(_p):
    def f(df):
        k = ind.keltner_channels(df["High"], df["Low"], df["Close"], 20, 20, 2.0)
        return _fill(df["Close"] <= k["mid"])
    return f


# family → {params grid, constraint, entry/exit builders (exit None = risk-overlay), category, min_bars, label}
ENTRY_FAMILIES: dict[str, dict] = {
    "ma_cross": {
        "params": {"fast": [10, 20, 50], "slow": [50, 100, 200]},
        "constraint": lambda p: p["fast"] < p["slow"],
        "entry": _ma_cross_entry, "exit": _ma_cross_exit, "category": "Trend", "min_bars": 210,
        "label": lambda p: f"MA cross {p['fast']}/{p['slow']}",
    },
    "dma_reclaim": {
        "params": {"n": [50, 100, 200]},
        "entry": _dma_reclaim_entry, "exit": _dma_reclaim_exit, "category": "Trend", "min_bars": 210,
        "label": lambda p: f"{p['n']}-DMA reclaim",
    },
    "macd_cross": {
        "params": {"trend_filter": [True, False]},
        "entry": _macd_cross_entry, "exit": _macd_cross_exit, "category": "Momentum", "min_bars": 210,
        "label": lambda p: "MACD cross" + (" (>200DMA)" if p["trend_filter"] else ""),
    },
    "roc_thrust": {
        "params": {"n": [21, 63], "th": [5, 10, 15]},
        "entry": _roc_entry, "exit": _roc_exit, "category": "Momentum", "min_bars": 260,
        "label": lambda p: f"ROC{p['n']}>{p['th']}%",
    },
    "supertrend_flip": {
        "params": {"period": [7, 10], "mult": [2, 3]},
        "entry": _supertrend_entry, "exit": _supertrend_exit, "category": "Trend", "min_bars": 60,
        "label": lambda p: f"Supertrend({p['period']},{p['mult']}) flip",
    },
    "adx_di": {
        "params": {"th": [20, 25]},
        "entry": _adx_entry, "exit": _adx_exit, "category": "Trend", "min_bars": 60,
        "label": lambda p: f"ADX>{p['th']} DI cross",
    },
    "rsi_reversion": {
        "params": {"n": [2, 14], "th": [10, 20, 30], "exit_th": [55, 65, 70]},
        "entry": _rsi_entry, "exit": _rsi_exit, "category": "Mean Reversion", "min_bars": 60,
        "label": lambda p: f"RSI{p['n']}<{p['th']}",
    },
    "bb_reversion": {
        "params": {"std": [2.0, 2.5]},
        "entry": _bb_reversion_entry, "exit": _bb_reversion_exit, "category": "Mean Reversion", "min_bars": 60,
        "label": lambda p: f"BB({p['std']}σ) snapback",
    },
    "williams_reversion": {
        "params": {"_": [0]},
        "entry": _williams_entry, "exit": _williams_exit, "category": "Mean Reversion", "min_bars": 60,
        "label": lambda _p: "Williams%R rebound",
    },
    "cci_reversion": {
        "params": {"_": [0]},
        "entry": _cci_entry, "exit": _cci_exit, "category": "Mean Reversion", "min_bars": 60,
        "label": lambda _p: "CCI oversold reversal",
    },
    "donchian_breakout": {
        "params": {"n": [20, 50]},
        "entry": _donchian_entry, "exit": None, "category": "Breakout", "min_bars": 60,
        "label": lambda p: f"{p['n']}-day Donchian breakout",
    },
    "keltner_breakout": {
        "params": {"_": [0]},
        "entry": _keltner_entry, "exit": None, "category": "Breakout", "min_bars": 260,
        "label": lambda _p: "Keltner breakout",
    },
}


# ── Spec → StrategyDef compiler ─────────────────────────────────────


def _spec_hash(spec: dict) -> str:
    canon = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canon.encode()).hexdigest()[:8]


def _risk_label(risk: dict) -> str:
    parts = []
    if risk.get("stop_loss_pct"):
        parts.append(f"stop {int(risk['stop_loss_pct'] * 100)}%")
    if risk.get("trailing_stop_pct"):
        parts.append(f"trail {int(risk['trailing_stop_pct'] * 100)}%")
    if risk.get("target_pct"):
        parts.append(f"target {int(risk['target_pct'] * 100)}%")
    if risk.get("max_holding_days"):
        parts.append(f"{risk['max_holding_days']}d")
    return ", ".join(parts) or "signal exit"


def build_strategy(spec: dict) -> StrategyDef:
    """Compile a discovery spec {entry:{kind,params}, risk:{...}} into a runnable StrategyDef.
    Deterministic: the same spec always yields the same id/name/behaviour."""
    ekind = spec["entry"]["kind"]
    fam = ENTRY_FAMILIES[ekind]
    eparams = spec["entry"]["params"]
    risk = spec.get("risk", {}) or {}
    entry_fn = fam["entry"](eparams)
    exit_fn = fam["exit"](eparams) if fam["exit"] is not None else _never
    label = fam["label"](eparams)
    return StrategyDef(
        id="disc-" + _spec_hash(spec),
        name=f"{label} · {_risk_label(risk)}",
        category=fam["category"],
        description=f"Discovered: enter on {label}; exit on "
                    + ("the paired signal" if fam["exit"] is not None else "risk stops")
                    + f" ({_risk_label(risk)}).",
        entry=entry_fn,
        exit=exit_fn,
        stop_loss_pct=risk.get("stop_loss_pct"),
        trailing_stop_pct=risk.get("trailing_stop_pct"),
        target_pct=risk.get("target_pct"),
        max_holding_days=risk.get("max_holding_days"),
        min_bars=fam.get("min_bars", 260),
    )


# ── Generation ──────────────────────────────────────────────────────


def _sample_risk(rng: random.Random, required: bool) -> dict:
    if required:  # breakout-style (no paired exit) MUST be able to close → force trail + time
        return {
            "stop_loss_pct": rng.choice([None, 0.08]),
            "trailing_stop_pct": rng.choice([0.08, 0.12]),
            "target_pct": None,
            "max_holding_days": rng.choice([20, 40, 60]),
        }
    return {
        "stop_loss_pct": rng.choice([None, 0.05, 0.08, 0.12]),
        "trailing_stop_pct": rng.choice([None, 0.08, 0.12]),
        "target_pct": rng.choice([None, 0.06, 0.10, 0.15]),
        "max_holding_days": rng.choice([None, 20, 40, 60]),
    }


def _sample_specs(n: int, seed: Optional[int] = None, families: Optional[list[str]] = None) -> list[dict]:
    """Random-search `n` unique specs from the grammar (deterministic given `seed`)."""
    rng = random.Random(seed)
    kinds = [k for k in ENTRY_FAMILIES if families is None or k in families] or list(ENTRY_FAMILIES)
    specs: dict[str, dict] = {}
    attempts = 0
    while len(specs) < n and attempts < n * 30:
        attempts += 1
        kind = rng.choice(kinds)
        fam = ENTRY_FAMILIES[kind]
        params = {p: rng.choice(opts) for p, opts in fam["params"].items()}
        if fam.get("constraint") and not fam["constraint"](params):
            continue
        risk = _sample_risk(rng, required=fam["exit"] is None)
        spec = {"entry": {"kind": kind, "params": params}, "risk": risk}
        specs[_spec_hash(spec)] = spec
    return list(specs.values())


# ── Fitness + keep-gate ─────────────────────────────────────────────


def _pf(m: dict) -> float:
    v = m.get("profit_factor")
    if v == "inf":
        return float("inf")
    return float(v or 0.0)


def _fitness(m: dict) -> float:
    """In-sample ranking score: reward edge AND sample size; trade-floor sinks the rest."""
    trades = m.get("trades", 0) or 0
    exp = m.get("expectancy_pct", 0.0) or 0.0
    if trades < MIN_TRADES_IS or exp <= 0:
        return -1e9
    return exp * math.sqrt(min(trades, 200))


def _is_promising(
    is_m: dict, oos_m: dict,
    min_trades_is: int = MIN_TRADES_IS, min_trades_oos: int = MIN_TRADES_OOS,
    min_sharpe: float = MIN_SHARPE, min_pf: float = MIN_PROFIT_FACTOR, degrade: float = DEGRADE_RATIO,
) -> bool:
    """Keep only strategies that clear the bar on BOTH slices and don't decay out-of-sample."""
    if (is_m.get("trades", 0) or 0) < min_trades_is or (oos_m.get("trades", 0) or 0) < min_trades_oos:
        return False
    for m in (is_m, oos_m):
        if (m.get("expectancy_pct", 0.0) or 0.0) <= 0:
            return False
        if (m.get("sharpe", 0.0) or 0.0) < min_sharpe:
            return False
        if _pf(m) < min_pf:
            return False
    is_sh = is_m.get("sharpe", 0.0) or 0.0
    oos_sh = oos_m.get("sharpe", 0.0) or 0.0
    if oos_sh < degrade * is_sh:          # degradation guard — the anti-overfit core
        return False
    return True


# ── Train/test split helpers ────────────────────────────────────────


def _all_dates(df_by_ticker: dict[str, pd.DataFrame]) -> Optional[pd.DatetimeIndex]:
    idx: Optional[pd.DatetimeIndex] = None
    for df in df_by_ticker.values():
        if df is None or df.empty:
            continue
        di = df.index.tz_localize(None) if df.index.tz is not None else df.index
        idx = di if idx is None else idx.union(di)
    return idx


def _split_date(df_by_ticker: dict[str, pd.DataFrame], frac: float):
    dates = _all_dates(df_by_ticker)
    if dates is None or len(dates) == 0:
        return None
    return dates[min(int(len(dates) * frac), len(dates) - 1)]


def _span_years(df_by_ticker: dict[str, pd.DataFrame], start, end) -> float:
    dates = _all_dates(df_by_ticker)
    if dates is None or len(dates) == 0:
        return 0.01
    lo = dates[0] if start is None else max(dates[0], pd.Timestamp(start))
    hi = dates[-1] if end is None else min(dates[-1], pd.Timestamp(end))
    return max((hi - lo).days / 365.25, 0.01)


# ── Persistence + catalog helpers ───────────────────────────────────


def _disc_file(market: str):
    return DISCOVERED_DIR / f"discovered_{market}.json"


def _persist_discovered(payload: dict, market: str) -> None:
    f = _disc_file(market)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(deep_sanitize(payload), indent=2, default=str))
    tmp.replace(f)


def load_discovered(market: str = "IN") -> Optional[dict]:
    f = _disc_file(market)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception as e:
        logger.warning("could not read discovered file for %s: %s", market, e)
        return None


def get_discovered_strategy(strategy_id: str, market: str = "IN") -> Optional[StrategyDef]:
    """Recompile a discovered strategy (promoted or not) from its stored spec, or None."""
    data = load_discovered(market)
    if not data:
        return None
    for s in data.get("survivors", []):
        if s.get("id") == strategy_id:
            try:
                return build_strategy(s["spec"])
            except Exception as e:
                logger.warning("could not rebuild discovered strategy %s: %s", strategy_id, e)
                return None
    return None


def promoted_strategies(market: str = "IN") -> list[StrategyDef]:
    """All promoted discovered strategies as runnable StrategyDefs (for runner inclusion)."""
    data = load_discovered(market)
    if not data:
        return []
    out: list[StrategyDef] = []
    for s in data.get("survivors", []):
        if s.get("promoted"):
            try:
                out.append(build_strategy(s["spec"]))
            except Exception:
                continue
    return out


def promote_discovered(strategy_id: str, market: str = "IN", promoted: bool = True) -> dict:
    data = load_discovered(market)
    if not data:
        return {"error": "No discovered strategies for this market"}
    found = False
    for s in data.get("survivors", []):
        if s.get("id") == strategy_id:
            s["promoted"] = bool(promoted)
            found = True
    if not found:
        return {"error": f"Discovered strategy {strategy_id} not found"}
    data["promoted_count"] = sum(1 for s in data.get("survivors", []) if s.get("promoted"))
    _persist_discovered(data, market)
    return {"status": "promoted" if promoted else "unpromoted", "id": strategy_id,
            "promoted_count": data["promoted_count"]}


def delete_discovered(strategy_id: str, market: str = "IN") -> dict:
    data = load_discovered(market)
    if not data:
        return {"error": "No discovered strategies for this market"}
    before = len(data.get("survivors", []))
    data["survivors"] = [s for s in data.get("survivors", []) if s.get("id") != strategy_id]
    if len(data["survivors"]) == before:
        return {"error": f"Discovered strategy {strategy_id} not found"}
    data["kept"] = len(data["survivors"])
    data["promoted_count"] = sum(1 for s in data["survivors"] if s.get("promoted"))
    _persist_discovered(data, market)
    return {"status": "deleted", "id": strategy_id, "remaining": len(data["survivors"])}


# ── Orchestration (background job) ──────────────────────────────────

discovery_job_state: dict = {
    "running": False, "stage": None, "progress_pct": 0.0, "detail": "",
    "started_at": None, "finished_at": None, "error": None, "market": None,
}


def _set_job(stage: str, pct: float, detail: str = "") -> None:
    discovery_job_state.update(stage=stage, progress_pct=round(pct, 1), detail=detail)


def _survivor_row(spec: dict, strat: StrategyDef, is_m: dict, oos_m: dict, fitness: float) -> dict:
    return {
        "id": strat.id, "name": strat.name, "category": strat.category,
        "description": strat.description, "spec": spec,
        "fitness": round(fitness, 4),
        "is_metrics": is_m, "oos_metrics": oos_m,
        "promoted": False,
    }


async def run_discovery(
    market: str = "IN",
    n_candidates: int = DEFAULT_N,
    families: Optional[list[str]] = None,
    split: float = DEFAULT_SPLIT,
    top_k: int = DEFAULT_TOP_K,
    seed: Optional[int] = None,
    years: int = 10,
    universe: Optional[str] = None,
    cost_bps: float = COST_BPS,
    min_trades_is: int = MIN_TRADES_IS,
    min_trades_oos: int = MIN_TRADES_OOS,
    min_sharpe: float = MIN_SHARPE,
    min_profit_factor: float = MIN_PROFIT_FACTOR,
    degrade_ratio: float = DEGRADE_RATIO,
) -> dict:
    """Generate → in-sample rank → out-of-sample validate → persist survivors. Background job.

    Loads the universe once (20h-cached), so a modest run is a few minutes. Returns a summary;
    the full catalog (with IS/OOS metrics + specs) is persisted to discovered_<MARKET>.json."""
    if discovery_job_state["running"]:
        return {"error": "A discovery job is already running"}

    from pipeline_core.markets import get_market

    m = get_market(market)
    index = universe or m.default_universe
    discovery_job_state.update(running=True, error=None, market=m.key,
                               started_at=datetime.now(timezone.utc).isoformat(), finished_at=None)
    _set_job("starting", 0.0, "")
    try:
        _set_job("loading universe", 3.0, index)
        tickers = await asyncio.to_thread(get_index_tickers, index, m.key)
        df_by_ticker, load_stats = await load_universe_history(
            tickers, years=years,
            progress_callback=lambda d, t: _set_job("loading universe", 3.0 + 22.0 * d / max(t, 1), f"{d}/{t}"),
            market=m.key)
        if not df_by_ticker:
            raise RuntimeError("No price history loaded — data source unreachable?")
        bench = await load_benchmark(m.benchmark, years, market=m.key)

        split_date = _split_date(df_by_ticker, split)
        is_years = _span_years(df_by_ticker, None, split_date)
        oos_years = _span_years(df_by_ticker, split_date, None)

        specs = _sample_specs(n_candidates, seed=seed, families=families)
        _set_job("in-sample scan", 26.0, f"{len(specs)} candidates")

        # In-sample: backtest every candidate, keep a fitness score.
        scored = []
        for i, spec in enumerate(specs):
            strat = build_strategy(spec)
            is_bt = await asyncio.to_thread(
                backtest_strategy, strat, df_by_ticker, bench, is_years, cost_bps, None, split_date)
            scored.append((_fitness(is_bt["metrics"]), spec, strat, is_bt["metrics"]))
            if i % 10 == 0:
                _set_job("in-sample scan", 26.0 + 50.0 * (i + 1) / len(specs), f"{i + 1}/{len(specs)}")

        scored.sort(key=lambda x: x[0], reverse=True)
        leaders = [s for s in scored[:top_k] if s[0] > -1e8]   # drop trade-floor failures

        # Out-of-sample: re-test the in-sample leaders on the held-out slice; keep survivors.
        survivors = []
        for j, (fit, spec, strat, is_m) in enumerate(leaders):
            oos_bt = await asyncio.to_thread(
                backtest_strategy, strat, df_by_ticker, bench, oos_years, cost_bps, split_date, None)
            oos_m = oos_bt["metrics"]
            if _is_promising(is_m, oos_m, min_trades_is, min_trades_oos, min_sharpe,
                             min_profit_factor, degrade_ratio):
                survivors.append(_survivor_row(spec, strat, is_m, oos_m, fit))
            _set_job("out-of-sample validation", 76.0 + 20.0 * (j + 1) / max(len(leaders), 1),
                     f"{j + 1}/{len(leaders)}")

        survivors.sort(key=lambda s: (s["oos_metrics"].get("sharpe", 0) or 0,
                                      s["oos_metrics"].get("expectancy_pct", 0) or 0), reverse=True)

        # Preserve any prior promotions when re-running (match by id).
        prior = {s["id"]: s.get("promoted", False) for s in (load_discovered(m.key) or {}).get("survivors", [])}
        for s in survivors:
            if prior.get(s["id"]):
                s["promoted"] = True

        payload = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "market": m.key,
            "params": {"n_candidates": n_candidates, "top_k": top_k, "split": split, "years": years,
                       "seed": seed, "cost_bps": cost_bps, "families": families,
                       "gate": {"min_trades_is": min_trades_is, "min_trades_oos": min_trades_oos,
                                "min_sharpe": min_sharpe, "min_profit_factor": min_profit_factor,
                                "degrade_ratio": degrade_ratio}},
            "coverage": {"tickers": len(df_by_ticker), "index": index,
                         "split_date": str(split_date.date()) if split_date is not None else None,
                         "is_years": round(is_years, 2), "oos_years": round(oos_years, 2)},
            "tried": len(specs),
            "validated_oos": len(leaders),
            "kept": len(survivors),
            "promoted_count": sum(1 for s in survivors if s.get("promoted")),
            "survivors": survivors,
            "note": (f"Sampled {len(specs)} strategies, in-sample-ranked, validated the top "
                     f"{len(leaders)} out-of-sample → kept {len(survivors)}. Survivors passed the "
                     f"gate on BOTH slices with no out-of-sample decay, but a positive backtest is "
                     f"still not proof — treat as candidates and confirm live before trusting."),
            "disclaimer": "Machine-discovered from historical OHLCV. Data-mined candidates; "
                          "multiple-testing means some survivors are luck. Not investment advice.",
        }
        _persist_discovered(payload, m.key)
        _set_job("done", 100.0, f"kept {len(survivors)}/{len(specs)}")
        return {"status": "completed", "market": m.key, "tried": len(specs),
                "validated_oos": len(leaders), "kept": len(survivors),
                "coverage": payload["coverage"]}
    except Exception as e:
        discovery_job_state["error"] = str(e)
        logger.exception("discovery run failed")
        return {"error": str(e)}
    finally:
        discovery_job_state.update(running=False, finished_at=datetime.now(timezone.utc).isoformat())


def status(market: str = "IN") -> dict:
    """Job state + a compact summary of the last persisted run for this market."""
    data = load_discovered(market)
    summary = None
    if data:
        summary = {k: data.get(k) for k in ("as_of", "market", "tried", "validated_oos", "kept",
                                            "promoted_count", "coverage", "params")}
    return {"job": dict(discovery_job_state), "last_run": summary}


def list_discovered(market: str = "IN") -> dict:
    """The full catalog for a market (survivors with IS/OOS metrics + promote flags)."""
    data = load_discovered(market)
    if not data:
        return {"market": market, "survivors": [], "kept": 0,
                "note": "No discovery run yet — run one to generate candidates."}
    return data
