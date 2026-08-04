"""Intraday bot backtest harness — triple-barrier replay, regime tag, edge aggregation,
closed-bar (no-repaint) fetch, JSON round-trip, and edge-weighted idea conviction.
Network-free (synthetic OHLCV, a controllable fake bot, mocked loaders)."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from pipeline_core.bots import backtest as bt
from pipeline_core.bots import ideas as ideas_mod
from pipeline_core.bots.base import make_signal
from pipeline_core.services import crypto_data as cd


def frame(closes, freq="15min", opens=None):
    c = np.asarray(closes, float)
    n = len(c)
    idx = pd.date_range("2026-01-01", periods=n, freq=freq)
    return pd.DataFrame({
        "Open": (np.asarray(opens, float) if opens is not None else c),
        "High": c * 1.006, "Low": c * 0.994, "Close": c, "Volume": np.full(n, 1e6),
    }, index=idx)


class FakeBot:
    """Fires the given signal exactly once (at the first evaluate), then stays flat —
    so a single deterministic trade drives the triple-barrier assertions."""
    needs_htf = needs_btc = False
    id, name, kind = "fake", "Fake", "signal"

    def __init__(self, side, entry, stop, target):
        self._s = (side, entry, stop, target)
        self.fired = False

    def evaluate(self, df, ctx):
        if self.fired:
            return make_signal("flat")
        self.fired = True
        return make_signal(*self._s)


def _one(bot, df):
    adx = np.full(len(df), 30.0)   # force "trend" regime unless a test overrides
    return bt.replay_bot(bot, df, "15m", adx, None, None, None, None, bt.COST_BPS / 1e4)


# ── Triple-barrier labeling ─────────────────────────────────────────


def test_long_hits_target():
    t = _one(FakeBot("long", 100, 95, 110), frame([100.0] * 62 + [116.0] * 58))
    assert len(t) == 1 and t[0]["reason"] == "target" and t[0]["win"]
    assert abs(t[0]["ret_pct"] - (10.0 - bt.COST_BPS / 100)) < 0.01     # +10% target − costs


def test_long_hits_stop():
    t = _one(FakeBot("long", 100, 95, 110), frame([100.0] * 62 + [90.0] * 58))
    assert t[0]["reason"] == "stop" and not t[0]["win"]
    assert t[0]["ret_pct"] < 0


def test_long_time_exit():
    t = _one(FakeBot("long", 100, 90, 115), frame([100.0] * 120))   # never touches either barrier
    assert t[0]["reason"] == "time" and t[0]["bars_held"] == bt.MAX_BARS["15m"]


def test_short_hits_target():
    t = _one(FakeBot("short", 100, 105, 90), frame([100.0] * 62 + [88.0] * 58))
    assert t[0]["reason"] == "target" and t[0]["win"] and t[0]["side"] == "short"
    assert abs(t[0]["ret_pct"] - (10.0 - bt.COST_BPS / 100)) < 0.01


def test_no_lookahead_entry_is_next_open():
    # Signal fires at bar 60; entry must be bar 61's OPEN (gapped away from bar 60's close).
    closes = [100.0] * 120
    opens = list(closes)
    opens[61] = 97.0                                   # gap on the entry bar
    t = _one(FakeBot("long", 100, 90, 115), frame(closes, opens=opens))
    assert t[0]["entry_px"] == 97.0 and t[0]["entry_date"].startswith(str(frame(closes).index[61]))


def test_regime_tag_from_adx():
    df = frame([100.0] * 62 + [116.0] * 58)
    low_adx = np.full(len(df), 10.0)                   # ranging
    t = bt.replay_bot(FakeBot("long", 100, 95, 110), df, "15m", low_adx, None, None, None, None, 0.0)
    assert t[0]["regime"] == "range"


# ── Aggregation ─────────────────────────────────────────────────────


def test_stats_math():
    trades = [{"ret_pct": 10, "reason": "target", "bars_held": 3, "regime": "trend", "side": "long"},
              {"ret_pct": -5, "reason": "stop", "bars_held": 2, "regime": "trend", "side": "long"},
              {"ret_pct": 10, "reason": "target", "bars_held": 4, "regime": "range", "side": "long"},
              {"ret_pct": -5, "reason": "stop", "bars_held": 2, "regime": "range", "side": "short"},
              {"ret_pct": 2, "reason": "time", "bars_held": 32, "regime": "trend", "side": "short"}]
    s = bt._stats(trades)
    assert s["trades"] == 5 and s["win_rate"] == 60.0
    assert s["expectancy_pct"] == 2.4 and s["profit_factor"] == 2.2
    assert s["target_exits"] == 2 and s["stop_exits"] == 2 and s["time_exits"] == 1
    b = bt._bundle(trades)
    assert set(b["by_regime"]) == {"trend", "range"} and set(b["by_side"]) == {"long", "short"}


def test_stats_empty():
    s = bt._stats([])
    assert s["trades"] == 0 and s["enough"] is False


# ── Persistence + lookup ────────────────────────────────────────────


def test_edge_json_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "EDGE_FILE", tmp_path / "edge.json")
    payload = {"as_of": "2026-01-01T00:00:00Z", "coverage": {"coins": 10},
               "bots": {"Fake|15m": bt._bundle([{"ret_pct": 3, "reason": "target",
                        "bars_held": 5, "regime": "trend", "side": "long"}] * 25)}}
    bt._persist_edge(payload)
    loaded = bt.load_edge()
    assert loaded["bots"]["Fake|15m"]["trades"] == 25
    st = bt.edge_for("Fake", "15m", edge=loaded)
    assert st and st["trades"] == 25


def test_edge_for_regime_fallback(monkeypatch):
    edge = {"bots": {"Fake|15m": {"trades": 40, "expectancy_pct": 1.0, "enough": True,
                                  "by_regime": {"trend": {"trades": 5, "enough": False}}}}}
    # Regime slice too small → falls back to the overall stats.
    assert bt.edge_for("Fake", "15m", "trend", edge=edge)["trades"] == 40


# ── Closed-bar (no-repaint) fetch ───────────────────────────────────


def test_get_klines_closed_only_drops_forming_bar(monkeypatch):
    full = frame(np.linspace(100, 110, 100), freq="15min")
    monkeypatch.setattr(cd, "_binance_klines", lambda s, i, b: full.copy())
    monkeypatch.setattr(cd.cache, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(cd.cache, "set", lambda *_a, **_k: None)
    closed = cd.get_klines("X", "15m", 200, closed_only=True)
    live = cd.get_klines("X", "15m", 200, closed_only=False)
    assert len(live) - len(closed) == 1                 # exactly the forming bar dropped
    assert closed.index[-1] == full.index[-2] and live.index[-1] == full.index[-1]


# ── Edge-weighted conviction (ideas) ────────────────────────────────


def test_edge_weight_maps_expectancy():
    edge = {"bots": {
        "A|15m": {"expectancy_pct": 1.5, "win_rate": 66, "enough": True, "trades": 40, "by_regime": {}},
        "B|15m": {"expectancy_pct": -0.3, "win_rate": 38, "enough": True, "trades": 40, "by_regime": {}}}}
    assert ideas_mod._edge_weight("A", "15m", "trend", edge)[0] == 3.0     # capped high
    assert ideas_mod._edge_weight("B", "15m", "trend", edge)[0] == 0.0     # negative → no weight
    assert ideas_mod._edge_weight("A", "15m", "trend", None)[0] == 1.0     # no backtest → neutral


@pytest.mark.asyncio
async def test_ideas_rank_by_edge(monkeypatch):
    down = frame(np.cumprod(1 + np.concatenate([[0], np.full(159, -0.004)])) * 150)
    monkeypatch.setattr(ideas_mod, "get_universe_tickers", lambda m, u: ["AAA"])
    monkeypatch.setattr(ideas_mod.crypto_data, "get_klines", lambda s, tf, bars, closed_only=True: down.copy())
    edge = {"as_of": "2026-01-01", "bots": {
        "EMA Trend|15m": {"expectancy_pct": 0.6, "win_rate": 60, "enough": True, "trades": 50, "by_regime": {}}}}
    monkeypatch.setattr(bt, "load_edge", lambda: edge)
    res = await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50")
    assert res["edge_weighted"] is True
    idea = res["short"][0]
    assert idea["edge_score"] is not None and idea["regime"] in ("trend", "range")


@pytest.mark.asyncio
async def test_ideas_fallback_without_edge(monkeypatch):
    down = frame(np.cumprod(1 + np.concatenate([[0], np.full(159, -0.004)])) * 150)
    monkeypatch.setattr(ideas_mod, "get_universe_tickers", lambda m, u: ["AAA"])
    monkeypatch.setattr(ideas_mod.crypto_data, "get_klines", lambda s, tf, bars, closed_only=True: down.copy())
    monkeypatch.setattr(bt, "load_edge", lambda: None)
    res = await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50")
    assert res["edge_weighted"] is False and res["short"][0]["edge_score"] is None
    assert res["short"][0]["quality"] is None and res["short"][0]["edge_backed"] is None


# ── Quality selectivity: regime eligibility, signed-quality ranking, strict gate ────


def _stat(exp, enough=True, trades=40):
    return {"expectancy_pct": exp, "win_rate": 55, "enough": enough, "trades": trades, "by_regime": {}}


def test_eligible_bots_prunes_worst_in_regime():
    edge = {"bots": {"Good|15m": _stat(0.5), "Mid|15m": _stat(0.0),
                     "Bad|15m": _stat(-0.6), "New|15m": _stat(0.0, enough=False, trades=5)}}
    # median of measured {0.5, 0.0, -0.6} = 0.0 → Bad (below median) pruned; New (unproven) kept neutral.
    elig = ideas_mod._eligible_bots(["Good", "Mid", "Bad", "New"], "15m", "trend", edge)
    assert elig == {"Good", "Mid", "New"}


def test_eligible_bots_absolute_floor():
    # Whole pack below the absolute floor → every *measured* bot disqualified regardless of median.
    edge = {"bots": {"A|15m": _stat(-0.4), "B|15m": _stat(-0.5), "C|15m": _stat(-0.35)}}
    assert ideas_mod._eligible_bots(["A", "B", "C"], "15m", "range", edge) == set()
    # No backtest → everyone eligible.
    assert ideas_mod._eligible_bots(["A", "B"], "15m", "range", None) == {"A", "B"}


def test_rank_key_orders_edge_backed_then_least_bad():
    key = ideas_mod._rank_key({"bots": {}})   # truthy edge
    ideas = [{"edge_backed": False, "quality": -0.30, "edge_score": 1, "conviction": 1, "r_multiple": 2},
             {"edge_backed": False, "quality": -0.10, "edge_score": 1, "conviction": 1, "r_multiple": 1},
             {"edge_backed": True, "quality": -0.50, "edge_score": 1, "conviction": 1, "r_multiple": 1}]
    ideas.sort(key=key, reverse=True)
    # Edge-backed first (even at worse quality), then least-bad among exploratory.
    assert [i["quality"] for i in ideas] == [-0.50, -0.10, -0.30]


def test_rank_key_no_edge_uses_conviction():
    key = ideas_mod._rank_key(None)
    ideas = [{"conviction": 1, "r_multiple": 5}, {"conviction": 3, "r_multiple": 1}]
    ideas.sort(key=key, reverse=True)
    assert [i["conviction"] for i in ideas] == [3, 1]


@pytest.mark.asyncio
async def test_edge_backed_flag(monkeypatch):
    down = frame(np.cumprod(1 + np.concatenate([[0], np.full(159, -0.004)])) * 150)
    monkeypatch.setattr(ideas_mod, "get_universe_tickers", lambda m, u: ["AAA"])
    monkeypatch.setattr(ideas_mod.crypto_data, "get_klines", lambda s, tf, bars, closed_only=True: down.copy())

    monkeypatch.setattr(bt, "load_edge", lambda: {"bots": {"EMA Trend|15m": _stat(0.6)}})
    pos = (await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50"))["short"][0]
    assert "EMA Trend" in pos["bots"] and pos["edge_backed"] is True and pos["quality"] == 0.6

    monkeypatch.setattr(bt, "load_edge", lambda: {"bots": {"EMA Trend|15m": _stat(-0.2)}})
    neg = (await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50"))["short"][0]
    assert neg["edge_backed"] is False and neg["quality"] == -0.2      # lone measured bot never self-prunes


@pytest.mark.asyncio
async def test_ideas_strict_hides_exploratory(monkeypatch):
    down = frame(np.cumprod(1 + np.concatenate([[0], np.full(159, -0.004)])) * 150)
    monkeypatch.setattr(ideas_mod, "get_universe_tickers", lambda m, u: ["AAA"])
    monkeypatch.setattr(ideas_mod.crypto_data, "get_klines", lambda s, tf, bars, closed_only=True: down.copy())
    monkeypatch.setattr(bt, "load_edge", lambda: {"bots": {"EMA Trend|15m": _stat(-0.2)}})   # all negative

    graceful = await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50", strict=False)
    assert graceful["short"] and graceful["short"][0]["edge_backed"] is False   # shown, labeled exploratory
    assert graceful["edge_backed"] == {"long": 0, "short": 0}

    strict = await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50", strict=True)
    assert strict["strict"] is True and strict["counts"] == {"long": 0, "short": 0}   # stand aside


@pytest.fixture
def anyio_backend():
    return "asyncio"
