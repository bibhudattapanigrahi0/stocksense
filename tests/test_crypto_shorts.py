"""Crypto SHORT ideas — the daily Trade Ideas board trades both ways for crypto only.

Covers the full short lifecycle end-to-end, network-free (synthetic OHLC + a monkeypatched
`load_latest`): the short-aware backtest engine, the crypto bearish-mirror strategy set, the
board pooling both directions, the side-aware tracker close/realized-P&L, the long-only basket,
and the directional pillar inversion. The long path stays untouched — asserted alongside."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from pipeline_core import adherence
from pipeline_core.backtest import basket, idea_ranking, runner
from pipeline_core.backtest.engine import build_daily_positions, simulate
from pipeline_core.backtest.strategies import StrategyDef, strategies_for_market
from pipeline_core.config import settings


def _frame(closes, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.003, "Low": c * 0.997, "Close": c, "Volume": 1e6}, index=idx)


def _entry_on(bar):
    def _e(df):
        s = pd.Series(False, index=df.index)
        s.iloc[bar] = True
        return s
    return _e


def _strat(direction="long", **over):
    base = dict(id=f"t-{direction}", name="t", category="Trend", description="",
                entry=_entry_on(5), exit=lambda df: pd.Series(False, index=df.index),
                min_bars=3, direction=direction)
    base.update(over)
    return StrategyDef(**base)


# ── Engine: short-aware simulate + portfolio ────────────────────────

def test_short_profits_on_a_falling_series():
    df = _frame(np.linspace(100, 60, 60))                     # −40% drift
    short = _strat("short", target_pct=0.15, stop_loss_pct=0.25)
    res = simulate("X", df, short, cost_roundtrip_bps=0)
    tr = res.trades[0] if res.trades else res.open_trade
    assert tr.side == "short"
    assert tr.exit_reason == "target"                         # falls into its (lower) target
    assert tr.ret_pct > 0                                     # a short gains when price falls


def test_short_return_is_the_exact_mirror_of_the_move():
    # ret_pct must equal (entry-exit)/entry — the same convention the tracker uses — not entry/exit-1.
    df = _frame(np.linspace(100, 60, 60))
    res = simulate("X", df, _strat("short", target_pct=0.15), cost_roundtrip_bps=0)
    tr = res.trades[0]
    mirror = (tr.entry_price - tr.exit_price) / tr.entry_price * 100
    assert tr.ret_pct == pytest.approx(mirror, abs=0.02)


def test_short_stops_out_when_price_rises():
    df = _frame(np.linspace(100, 140, 60))                    # +40% drift → a short loses
    short = _strat("short", stop_loss_pct=0.15, target_pct=0.30)
    res = simulate("X", df, short, cost_roundtrip_bps=0)
    tr = res.trades[0] if res.trades else res.open_trade
    assert tr.exit_reason == "stop" and tr.ret_pct < 0


def test_long_path_is_unchanged_on_the_same_series():
    df = _frame(np.linspace(100, 60, 60))
    res = simulate("X", df, _strat("long", target_pct=0.15, stop_loss_pct=0.20), cost_roundtrip_bps=0)
    tr = res.trades[0] if res.trades else res.open_trade
    assert tr.side == "long" and tr.exit_reason == "stop" and tr.ret_pct < 0   # a long loses as price falls


def test_build_daily_positions_negates_short_returns():
    df = _frame(np.linspace(100, 60, 60))
    res = simulate("X", df, _strat("short", target_pct=0.9), cost_roundtrip_bps=0)   # stay in position
    dp = build_daily_positions({"X": res}, {"X": df})
    assert dp.mean() > 0                                      # falling price → the short book rises


# ── Strategy library: crypto short set, gated, other markets long-only ──

def test_crypto_runs_a_short_set_but_other_markets_do_not():
    crypto_shorts = [s for s in strategies_for_market("CRYPTO") if s.direction == "short"]
    assert len(crypto_shorts) == 3
    assert all(s.direction == "long" for s in strategies_for_market("US"))
    assert all(s.direction == "long" for s in strategies_for_market("IN"))


def test_flag_off_drops_the_crypto_short_strategies(monkeypatch):
    monkeypatch.setattr(settings, "crypto_shorts_enabled", False)
    assert not any(s.direction == "short" for s in strategies_for_market("CRYPTO"))


# ── Tracker: side-aware close, realized P&L, target fallback ─────────

def _short_idea(**over):
    base = dict(ticker="ETH-USD", market="CRYPTO", horizon="medium", side="short",
                entry_price=100.0, stop_price=115.0, target_price=90.0,
                signal_date="2026-06-01", tracked_at="2026-06-01T00:00:00+00:00")
    base.update(over)
    return base


def _hist(closes, start="2026-06-01"):
    idx = pd.date_range(start, periods=len(closes), freq="B")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c}, index=idx)


def test_short_closes_on_target_when_price_falls():
    out = runner._evaluate_idea_path(_short_idea(), _hist([100, 96, 91, 88, 92]))
    assert out["closed"] and out["exit_reason"] == "target"
    assert out["exit_price"] == 90.0 and out["realized_return_pct"] == 10.0   # (100-90)/100


def test_short_closes_on_stop_when_price_rises():
    out = runner._evaluate_idea_path(_short_idea(), _hist([100, 108, 116, 112]))
    assert out["closed"] and out["exit_reason"] == "stop"
    assert out["exit_price"] == 115.0 and out["realized_return_pct"] == -15.0  # (100-115)/100


def test_short_effective_target_is_below_entry():
    assert runner._effective_target(_short_idea(target_price=None, target_pct=0.15)) == 85.0


def test_long_tracker_path_still_correct():
    long_idea = dict(ticker="BTC", market="CRYPTO", horizon="medium", entry_price=100.0,
                     stop_price=95.0, target_price=110.0, signal_date="2026-06-01")
    out = runner._evaluate_idea_path(long_idea, _hist([100, 102, 105, 111, 108]))
    assert out["exit_reason"] == "target" and out["realized_return_pct"] == 10.0


# ── Board: horizon_ideas pools both directions for crypto ───────────

def _crypto_latest():
    """A synthetic persisted crypto run with one long + one short strategy, each firing a buy signal."""
    def _strat_payload(sid, ticker, price):
        return {"id": sid, "name": sid, "category": "Momentum",
                "metrics": {"win_rate": 55.0, "sharpe": 1.0, "expectancy_pct": 2.0,
                            "max_drawdown_pct": -30.0},
                "signals": {"buy": [{"ticker": ticker, "price": price, "signal_date": "2026-06-01"}],
                            "sell": [], "holding": []}}
    return {
        "run_at": "2026-06-01T00:00:00+00:00", "market": "CRYPTO", "index": "Top 30",
        "benchmark_label": "Bitcoin", "years": 2, "disclaimer": "",
        "strategies": [
            _strat_payload("crypto-momentum-regime", "BTC-USD", 100.0),        # long
            _strat_payload("crypto-momentum-down-regime", "ETH-USD", 200.0),   # short
        ],
    }


def test_crypto_board_carries_both_directions(monkeypatch):
    monkeypatch.setattr(runner, "_load_tracked", lambda: [])
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _crypto_latest())
    ideas = runner.horizon_ideas(market="CRYPTO")
    cands = [c for h in ideas["horizons"] for c in ideas["buckets"][h]["candidates"]]
    longs = [c for c in cands if c.get("side") != "short"]
    shorts = [c for c in cands if c.get("side") == "short"]
    assert any(c["ticker"] == "BTC-USD" for c in longs)
    assert shorts, "expected a short candidate on the crypto board"
    s = shorts[0]
    assert s["ticker"] == "ETH-USD"
    # Short geometry: stop above entry, target below.
    assert s["stop_price"] > s["price"] > s["target_price"]


def test_crypto_board_has_no_shorts_when_flag_off(monkeypatch):
    monkeypatch.setattr(runner, "_load_tracked", lambda: [])
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _crypto_latest())
    monkeypatch.setattr(settings, "crypto_shorts_enabled", False)
    ideas = runner.horizon_ideas(market="CRYPTO")
    cands = [c for h in ideas["horizons"] for c in ideas["buckets"][h]["candidates"]]
    assert all(c.get("side") != "short" for c in cands)


def test_enriched_board_shorts_have_live_levels_and_basket_stays_long(monkeypatch):
    # Live-enrich the crypto board (recompose per side + live_levels + ride-along basket). A quote is
    # returned so live_levels are computed and their short geometry can be checked.
    monkeypatch.setattr(runner, "_load_tracked", lambda: [])
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _crypto_latest())
    monkeypatch.setattr(runner, "get_quote", lambda sym, market="IN", max_age=None:
                        {"BTC-USD": 100.0, "ETH-USD": 200.0}.get(sym))
    ideas = runner.horizon_ideas(market="CRYPTO")
    ideas = asyncio.run(runner.enrich_ideas_live(ideas, "CRYPTO"))
    cands = [c for h in ideas["horizons"] for c in ideas["buckets"][h]["candidates"]]
    short = next(c for c in cands if c.get("side") == "short")
    ll = short["live_levels"]
    assert ll["stop_price"] > short["live_price"] > ll["target_price"]     # short live geometry
    # The ride-along basket is long-only.
    assert all(it.get("side") != "short" for it in (ideas.get("basket") or {}).get("items", []))


# ── Basket stays long-only ──────────────────────────────────────────

def test_basket_excludes_shorts():
    def _cand(ticker, side="long"):
        return {"ticker": ticker, "side": side, "price": 100.0, "buy_low": 99.0, "buy_high": 101.0,
                "stop_pct": 0.06, "stop_price": 94.0, "target_pct": 0.12, "target_price": 112.0,
                "risk_reward": 2.0, "conviction": 3, "eligible_conviction": 3, "avg_win_rate": 55.0,
                "rank_score": 3.0, "sector": None, "zone": "in_zone", "is_new": False}
    board = {"market": "CRYPTO", "horizons": ["short", "medium", "long"],
             "buckets": {h: {"horizon": h, "label": h.title(),
                             "candidates": ([_cand("BTC-USD"), _cand("ETH-USD", "short")]
                                            if h == "medium" else [])}
                         for h in ["short", "medium", "long"]}}
    b = basket.build_basket(board, None)
    assert all(it.get("side") != "short" for it in b.get("items", []))
    assert any(it["ticker"] == "BTC-USD" for it in b.get("items", []))    # the long survives


# ── Directional pillars invert for shorts ───────────────────────────

def test_adherence_scores_a_short_with_inverted_sign():
    # A short at 100 whose plan targets 90: covering at 85 (below the plan) is BETTER than the plan —
    # "early_saved" and a positive return — the opposite of what the long-only math would say.
    assert adherence._pct(100.0, 85.0, "short") == 15.0                    # short gains as price falls
    assert adherence._pct(100.0, 85.0, "long") == -15.0                    # long convention unchanged
    d = adherence.exit_delta(100.0, your_exit=85.0, plan_exit=90.0, side="short")
    assert d["verdict"] == "early_saved" and d["delta_pct"] == 5.0
    # Covering ABOVE the plan (95 vs 90) costs you on a short.
    assert adherence.exit_delta(100.0, 95.0, 90.0, side="short")["verdict"] == "early_cost"


def test_short_flips_directional_pillars():
    # Smart money ACCUMULATING supports a long but argues against a short; regime_fit is already
    # side-correct so it is read as-is.
    c = {"conviction": 2, "eligible_conviction": 2,
         "smart_money": {"available": True, "direction": "accumulate"}, "regime_fit": "tailwind"}
    long_p = idea_ranking.idea_pillars(c, side="long")
    short_p = idea_ranking.idea_pillars(c, side="short")
    assert long_p["smart_money"] is True and short_p["smart_money"] is False
    assert long_p["regime"] is True and short_p["regime"] is True         # regime not flipped
    assert long_p["technical"] is True and short_p["technical"] is True
