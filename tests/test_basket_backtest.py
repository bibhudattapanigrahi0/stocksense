"""Basket-method backtest (held-basket, weekly rebalance) + basket→portfolio deploy. Offline.

The simulation core (`simulate_basket`) is pure, so we drive it with synthetic trending / flat
universes and assert the honest signal: trending → positive CAGR, flat → ≈0. Plus point-in-time
(anti-lookahead) unit checks and the deploy mapping."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from pipeline_core.backtest import basket_backtest as bb
from pipeline_core.backtest.strategies import StrategyDef


# ── Synthetic strategies (causal entries) + universe ────────────────

def _sma_entry(df):
    return (df["Close"] > df["Close"].rolling(20, min_periods=20).mean()).fillna(False)


def _mom_entry(df):
    return (df["Close"] > df["Close"].shift(20)).fillna(False)


def _never(df):
    return pd.Series(False, index=df.index)


STRAT_SHORT = StrategyDef(id="t-sma", name="SMA>", category="Trend", description="",
                          entry=_sma_entry, exit=_never, max_holding_days=10,
                          stop_loss_pct=0.06, target_pct=0.12)
STRAT_MED = StrategyDef(id="t-mom", name="Mom20", category="Momentum", description="",
                        entry=_mom_entry, exit=_never, max_holding_days=30,
                        stop_loss_pct=0.08, target_pct=0.16)


def _calendar(n):
    return pd.bdate_range("2015-01-01", periods=n)


def _frame(cal, series):
    return pd.DataFrame({"Close": series}, index=cal)


def _trending_universe(n=340, k=6):
    cal = _calendar(n)
    rng = np.random.default_rng(1)
    frames = {}
    for j in range(k):
        drift = 1.0 + 0.0012 * (1 + j % 3)                  # steady uptrend, varied per name
        px = 100.0 * drift ** np.arange(n) * (1 + rng.normal(0, 0.004, n))
        frames[f"T{j}"] = _frame(cal, px)
    bench = _frame(cal, 100.0 * 1.0008 ** np.arange(n))     # benchmark also rising → risk-on
    return frames, bench, cal


def _flat_universe(n=340, k=6):
    cal = _calendar(n)
    rng = np.random.default_rng(2)
    frames = {f"F{j}": _frame(cal, 100.0 + rng.normal(0, 0.5, n).cumsum() * 0.02) for j in range(k)}
    bench = _frame(cal, 100.0 + rng.normal(0, 0.3, n).cumsum() * 0.02)
    return frames, bench, cal


STRATS = [STRAT_SHORT, STRAT_MED]


# ── Anti-lookahead / point-in-time units ────────────────────────────

def test_rolling_fired_is_causal():
    cal = _calendar(30)
    sig = pd.Series(False, index=cal)
    sig.iloc[10] = True                                     # fires only on bar 10
    out = bb._rolling_fired(sig, cal, lookback=3)
    assert not out[:10].any()                               # nothing before the fire (no future leak)
    assert out[10] and out[11] and out[12]                  # True across the lookback window
    assert not out[13]                                      # then it lapses


def test_regime_asof_flips_on_benchmark_trend():
    up = np.linspace(80, 120, 260)                          # price climbing above its 200-DMA
    down = np.linspace(120, 80, 260)
    assert bb._regime_asof(up, 259)["state"] == "risk-on"
    assert bb._regime_asof(down, 259)["state"] == "risk-off"


def test_reconstruct_board_counts_conviction():
    frames, bench, cal = _trending_universe()
    closes = {tk: df["Close"].to_numpy() for tk, df in frames.items()}
    fired = {(s.id, tk): bb._rolling_fired(s.entry(frames[tk]), cal, 3) for s in STRATS for tk in frames}
    i = 300
    board = bb._reconstruct_board(frames, closes, fired, STRATS, i, cal[i],
                                  {"state": "risk-on", "tilt": 0.3}, "IN")
    names = {c["ticker"]: c for h in board["horizons"] for c in board["buckets"][h]["candidates"]}
    assert names                                            # uptrend → strategies flag names
    # Both strategies fire on the trenders → conviction up to 2 across the two horizons for a name.
    assert max(c["conviction"] for c in names.values()) >= 1
    for c in names.values():
        assert c["stop_price"] and c["target_price"] and c["rank_score"] == c["conviction"]


# ── The simulation core ─────────────────────────────────────────────

def test_simulate_trending_is_profitable_and_wellformed():
    frames, bench, cal = _trending_universe()
    r = bb.simulate_basket(frames, bench, STRATS, market="IN", rebalance_days=5, warmup=60, cost_bps=25)
    assert r["ran"]
    assert r["metrics"]["cagr_pct"] > 0                     # a rising universe held long → positive
    assert r["extras"]["rebalances"] > 10 and r["extras"]["avg_turnover_pct"] >= 0
    assert r["extras"]["avg_basket_size"] > 0
    curve = r["equity_curve"]
    assert curve and all({"date", "nav", "benchmark_nav"} <= set(pt) for pt in curve)
    assert curve[-1]["nav"] > curve[0]["nav"]               # equity grew
    m = r["metrics"]
    for key in ("cagr_pct", "sharpe", "max_drawdown_pct", "alpha_vs_benchmark_pct"):
        assert key in m and m[key] is not None


def test_simulate_flat_is_not_wildly_positive():
    frames, bench, cal = _flat_universe()
    r = bb.simulate_basket(frames, bench, STRATS, market="IN", rebalance_days=5, warmup=60, cost_bps=25)
    assert r["ran"]
    assert -20.0 < r["metrics"]["cagr_pct"] < 20.0          # no drift → no spurious edge


def test_simulate_insufficient_history():
    cal = _calendar(50)
    frames = {"A": _frame(cal, np.linspace(100, 110, 50))}
    bench = _frame(cal, np.linspace(100, 105, 50))
    r = bb.simulate_basket(frames, bench, STRATS, market="IN", warmup=60)
    assert r["ran"] is False


def test_hysteresis_reduces_turnover():
    frames, bench, cal = _trending_universe()
    hi = bb.simulate_basket(frames, bench, STRATS, market="IN", rebalance_days=5, warmup=60)
    # With a huge hysteresis margin an incumbent is nearly unbeatable → lower turnover.
    import pipeline_core.backtest.basket as bmod
    saved = bmod.settings.basket_hysteresis_margin
    try:
        bmod.settings.basket_hysteresis_margin = 5.0
        sticky = bb.simulate_basket(frames, bench, STRATS, market="IN", rebalance_days=5, warmup=60)
    finally:
        bmod.settings.basket_hysteresis_margin = saved
    assert sticky["extras"]["avg_turnover_pct"] <= hi["extras"]["avg_turnover_pct"] + 1e-6


# ── Phase 1: deploy the basket as a Portfolio ───────────────────────

def _canned_board():
    return {"market": "US", "basket": {
        "items": [
            {"ticker": "AAA", "horizon": "short", "horizon_label": "Short term", "alloc_pct": 18.8,
             "price": 100.0, "live_price": 101.0, "stop_pct": 0.06, "stop_price": 94.0,
             "target_pct": 0.12, "conviction": 3, "basket_score": 0.8, "expected_return_pct": 4.0,
             "win_prob": 60.0, "sector": "Tech"},
            {"ticker": "BBB", "horizon": "medium", "horizon_label": "Medium term", "alloc_pct": 9.0,
             "price": 50.0, "stop_pct": 0.06, "stop_price": 47.0, "target_pct": 0.12,
             "conviction": 2, "basket_score": 0.7},
        ],
        "summary": {"cash_pct": 72.2, "count": 2, "expected_return_pct": 2.4, "avg_win_pct": 58.0},
        "regime_state": "risk-on", "note": "estimate not advice"}}


def _patch_deploy(monkeypatch, existing=None):
    from pipeline_core.backtest import runner
    from pipeline_core.portfolio import track
    from pipeline_core.services import run_store
    board = _canned_board()
    monkeypatch.setattr(runner, "horizon_ideas", lambda mpb, mk: {"market": "US"})

    async def fake_enrich(ideas, mk):
        return board
    monkeypatch.setattr(runner, "enrich_ideas_live", fake_enrich)

    saved = {}

    async def fake_get(name, user_id=None):
        return existing
    async def fake_save(pf, user_id=None):
        pf.id = pf.id or "pf1"
        saved["pf"] = pf
        return pf
    async def fake_snap(pf, **k):
        return {"nav": 1.0, "positions_total": len(pf.positions)}
    monkeypatch.setattr(run_store, "get_portfolio", fake_get)
    monkeypatch.setattr(run_store, "save_portfolio", fake_save)
    monkeypatch.setattr(track, "snapshot_portfolio", fake_snap)
    return runner, saved


def test_deploy_basket_maps_positions_and_cash(monkeypatch):
    runner, saved = _patch_deploy(monkeypatch)
    res = asyncio.run(runner.deploy_basket("US"))
    assert res["deployed"] and res["portfolio_id"] == "pf1"
    pf = saved["pf"]
    assert [p.ticker for p in pf.positions] == ["AAA", "BBB"]
    assert pf.positions[0].weight == pytest.approx(0.188)   # alloc_pct/100
    assert pf.positions[0].entry_price == 101.0             # live_price preferred over signal price
    assert pf.cash_pct == pytest.approx(0.722)
    assert pf.spec.strategy_ids == ["__basket__"]
    assert pf.summary["source"] == "basket"


def test_deploy_basket_preserves_cost_basis_on_redeploy(monkeypatch):
    from pipeline_core.portfolio.models import Portfolio, PortfolioPosition, PortfolioSpec
    prior = Portfolio(id="pf1", name="Today's Basket — US",
                      spec=PortfolioSpec(name="Today's Basket — US", market="US"),
                      positions=[PortfolioPosition(ticker="AAA", weight=0.2, entry_price=90.0)])
    runner, saved = _patch_deploy(monkeypatch, existing=prior)
    asyncio.run(runner.deploy_basket("US"))
    aaa = next(p for p in saved["pf"].positions if p.ticker == "AAA")
    assert aaa.entry_price == 90.0                          # held name keeps its original cost basis
