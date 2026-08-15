"""Phase 1 trade-idea efficiency levers — network-free.

Covers the rank-efficiency north-star metric (1A), the realistic cost + crypto short funding model
(1B), the crypto-shorts-watchlist gate (1C), and the survivorship win-rate haircut (1D). See
docs/idea-efficiency-roadmap.md."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from pipeline_core.backtest import idea_ranking as ir, runner
from pipeline_core.backtest.engine import simulate
from pipeline_core.backtest.strategies import StrategyDef
from pipeline_core.config import market_cost_bps, settings


# ── 1A. Rank efficiency (rank-IC + decile spread) ───────────────────

def test_rank_ic_positive_when_rank_predicts_return():
    closed = [{"entry_rank_pct": p / 10, "realized_return_pct": p} for p in range(11)]
    r = ir.rank_efficiency(closed)
    assert r["active"] and r["ic"] > 0.9 and r["spread_pct"] > 0
    assert r["top_quintile_return_pct"] > r["bottom_quintile_return_pct"]


def test_rank_ic_negative_when_ranking_inverted():
    closed = [{"entry_rank_pct": p / 10, "realized_return_pct": -p} for p in range(11)]
    r = ir.rank_efficiency(closed)
    assert r["ic"] < -0.9 and r["spread_pct"] < 0


def test_rank_ic_cold_start_and_skips_records_without_rank():
    assert ir.rank_efficiency([{"entry_rank_pct": 0.5, "realized_return_pct": 1.0}])["active"] is False
    # Records predating entry_rank_pct are simply excluded (not counted).
    mixed = [{"realized_return_pct": 5.0}] * 20                       # no entry_rank_pct
    assert ir.rank_efficiency(mixed)["n"] == 0


# ── 1B. Realistic cost + crypto short funding ───────────────────────

def test_market_cost_bps_differs_by_market():
    assert market_cost_bps("CRYPTO") > market_cost_bps("US")         # crypto spreads are wider
    assert market_cost_bps("IN") == settings.cost_bps_in
    assert market_cost_bps(None) == settings.cost_bps_in             # default IN


def _falling(n=40):
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    c = pd.Series(np.linspace(100, 70, n), index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.003, "Low": c * 0.997, "Close": c, "Volume": 1e6}, index=idx)


def test_short_pays_funding_while_held():
    df = _falling()
    # A short with no fixed exit → rides to end-of-data, so it's held many bars and funding bites.
    short = StrategyDef(id="sf", name="sf", category="Trend", description="",
                        entry=lambda d: pd.Series([i == 5 for i in range(len(d))], index=d.index),
                        exit=lambda d: pd.Series(False, index=d.index), min_bars=3, direction="short")
    free = simulate("X", df, short, cost_roundtrip_bps=0, funding_bps_per_day=0.0)
    paid = simulate("X", df, short, cost_roundtrip_bps=0, funding_bps_per_day=50.0)
    r_free = (free.trades or [free.open_trade])[0].ret_pct
    r_paid = (paid.trades or [paid.open_trade])[0].ret_pct
    assert r_paid < r_free                                            # funding is a real drag on a held short


def test_long_never_pays_funding():
    df = _falling()
    long = StrategyDef(id="lf", name="lf", category="Trend", description="",
                       entry=lambda d: pd.Series([i == 5 for i in range(len(d))], index=d.index),
                       exit=lambda d: pd.Series(False, index=d.index), min_bars=3)   # long
    a = simulate("X", df, long, cost_roundtrip_bps=0, funding_bps_per_day=0.0)
    b = simulate("X", df, long, cost_roundtrip_bps=0, funding_bps_per_day=50.0)
    assert (a.trades or [a.open_trade])[0].ret_pct == (b.trades or [b.open_trade])[0].ret_pct


# ── Shared synthetic crypto board (long + short) ────────────────────

def _crypto_latest():
    def strat(sid, ticker, price, wr):
        return {"id": sid, "name": sid, "category": "Momentum",
                "metrics": {"win_rate": wr, "sharpe": 1.1, "expectancy_pct": 3.0,
                            "max_drawdown_pct": -30.0},
                "signals": {"buy": [{"ticker": ticker, "price": price, "signal_date": "2026-08-10"}],
                            "sell": [], "holding": []}}
    return {"run_at": "2026-08-10T00:00:00+00:00", "market": "CRYPTO", "index": "Top 30",
            "benchmark_label": "Bitcoin", "years": 2, "disclaimer": "", "cost_roundtrip_bps": 35.0,
            "funding_bps_per_day": 10.0,
            "strategies": [strat("crypto-momentum-regime", "BTC-USD", 100.0, 60.0),
                           strat("crypto-momentum-down-regime", "ETH-USD", 200.0, 58.0)]}


def _board(monkeypatch):
    monkeypatch.setattr(runner, "_load_tracked", lambda: [])
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _crypto_latest())
    monkeypatch.setattr(runner, "get_quote", lambda sym, market="IN", max_age=None:
                        {"BTC-USD": 100.0, "ETH-USD": 200.0}.get(sym))
    ideas = runner.horizon_ideas(market="CRYPTO")
    return asyncio.run(runner.enrich_ideas_live(ideas, "CRYPTO"))


# ── 1C. Crypto shorts gated to the watchlist until proven ───────────

def test_shorts_never_actionable_when_gate_on(monkeypatch):
    monkeypatch.setattr(settings, "crypto_shorts_actionable", False)
    ideas = _board(monkeypatch)
    shorts = [c for h in ideas["horizons"] for c in ideas["buckets"][h]["candidates"]
              if c.get("side") == "short"]
    assert shorts and all(c.get("tier") != "actionable" for c in shorts)
    assert all(c.get("short_gated") for c in shorts)


def test_gate_lifts_when_shorts_proven(monkeypatch):
    monkeypatch.setattr(settings, "crypto_shorts_actionable", True)
    ideas = _board(monkeypatch)
    shorts = [c for h in ideas["horizons"] for c in ideas["buckets"][h]["candidates"]
              if c.get("side") == "short"]
    assert shorts and not any(c.get("short_gated") for c in shorts)   # gate no longer forces watch


# ── 1D. Survivorship haircut ────────────────────────────────────────

def test_survivorship_haircut_lowers_board_win_rate(monkeypatch):
    monkeypatch.setattr(settings, "survivorship_haircut_pp", 5.0)
    ideas = _board(monkeypatch)
    btc = next(c for h in ideas["horizons"] for c in ideas["buckets"][h]["candidates"]
               if c["ticker"] == "BTC-USD")
    assert btc["avg_win_rate"] == pytest.approx(60.0 - 5.0, abs=0.01)   # 60 raw − 5pp haircut
    assert ideas["cost_disclosure"]["survivorship_haircut_pp"] == 5.0
    assert ideas["cost_disclosure"]["cost_bps"] == 35.0                 # disclosed from the run


def test_no_haircut_leaves_win_rate_raw(monkeypatch):
    monkeypatch.setattr(settings, "survivorship_haircut_pp", 0.0)
    ideas = _board(monkeypatch)
    btc = next(c for h in ideas["horizons"] for c in ideas["buckets"][h]["candidates"]
               if c["ticker"] == "BTC-USD")
    assert btc["avg_win_rate"] == pytest.approx(60.0, abs=0.01)


# ── 1A persistence: the tracker must capture entry rank, or rank-efficiency can never activate ──

def test_snapshot_persists_entry_rank(monkeypatch):
    monkeypatch.setattr(runner, "_load_tracked", lambda: [])
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _crypto_latest())
    monkeypatch.setattr(runner, "get_quote", lambda sym, market="IN", max_age=None:
                        {"BTC-USD": 100.0, "ETH-USD": 200.0}.get(sym))
    saved = {}
    monkeypatch.setattr(runner, "_save_tracked", lambda items: saved.update(items=items))
    monkeypatch.setattr(runner, "_save_basket", lambda mk, payload: None)
    asyncio.run(runner.snapshot_current_ideas(market="CRYPTO"))
    recs = saved.get("items", [])
    assert recs, "expected tracked records written"
    for r in recs:
        assert r.get("rank_score") is not None
        assert 0.0 <= r.get("entry_rank_pct") <= 1.0
