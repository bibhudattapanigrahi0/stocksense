"""Cross-market regime gate — the per-market risk-on/off gauge and its effect on the daily ideas.

Covers the pure gauge (benchmark 200-DMA trend + vol index / DXY-inversion / crypto Fear & Greed,
with neutral degradation) and the end-to-end `horizon_ideas` wiring: in risk-off a counter-cyclical
(mean-reversion) long outranks a pro-cyclical (trend) one and the book's size is trimmed, while a
neutral reading (the default) leaves ordering and sizing exactly as they were.

Network-free: benchmark/VIX/DXY history and Fear & Greed are monkeypatched. The real gauge is bound
at import (`real_market_regime`) so the conftest fixture that stubs the module attribute doesn't hide
the live-fetch code path under test."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from pipeline_core.backtest import runner
from pipeline_core.services import market_regime as mr
from pipeline_core.services.market_regime import market_regime as real_market_regime


# ── Synthetic price frames ──────────────────────────────────────────


def _frame(closes):
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    c = np.asarray(closes, float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                         "Volume": np.full(len(c), 1e6)}, index=idx)


UP = _frame(np.linspace(100, 200, 260))       # last close well above its 200-DMA
DOWN = _frame(np.linspace(200, 100, 260))      # last close well below its 200-DMA


def _flat(level):
    return _frame(np.full(260, float(level)))   # constant vol index reading


def _bypass_cache(monkeypatch):
    monkeypatch.setattr(mr._cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(mr._cache, "set", lambda *a, **k: None)


def _patch_prices(monkeypatch, mapping):
    from pipeline_core.services import market_data

    def fake(symbol, period="1y", interval="1d", market="IN"):
        return mapping.get(symbol, pd.DataFrame())

    monkeypatch.setattr(market_data, "get_price_history", fake)


# ── 1. The gauge, per market ────────────────────────────────────────


def test_equity_risk_on(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_prices(monkeypatch, {"^GSPC": UP, "^VIX": _flat(12)})     # uptrend + calm VIX
    r = real_market_regime("US")
    assert r["state"] == "risk-on" and r["tilt"] > 0 and r["stale"] is False
    assert r["benchmark"]["trend"] == "up" and r["volatility"]["level"] == "calm"


def test_equity_risk_off(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_prices(monkeypatch, {"^GSPC": DOWN, "^VIX": _flat(35)})   # downtrend + stressed VIX
    r = real_market_regime("US")
    assert r["state"] == "risk-off" and r["tilt"] < 0
    assert r["benchmark"]["trend"] == "down" and r["volatility"]["level"] == "high"


def test_metals_dxy_inverted(monkeypatch):
    _bypass_cache(monkeypatch)
    # Gold up either way; a FALLING dollar is a metals tailwind, a RISING dollar a headwind.
    _patch_prices(monkeypatch, {"GC=F": UP, "DX-Y.NYB": DOWN})
    tail = real_market_regime("METALS")
    _patch_prices(monkeypatch, {"GC=F": UP, "DX-Y.NYB": UP})
    head = real_market_regime("METALS")
    assert tail["tilt"] > head["tilt"]                              # DXY-down friendlier than DXY-up
    assert tail["state"] == "risk-on"


def test_equity_macro_credit_shifts_tilt(monkeypatch):
    _bypass_cache(monkeypatch)
    base = {"^GSPC": UP, "^VIX": _flat(18)}                          # same trend + vol either way
    # Firm credit (HYG/LQD ratio rising) should read more risk-on than stressed (ratio falling).
    _patch_prices(monkeypatch, {**base, "HYG": UP, "LQD": _flat(100)})
    firm = real_market_regime("US")
    _patch_prices(monkeypatch, {**base, "HYG": DOWN, "LQD": _flat(100)})
    stressed = real_market_regime("US")
    assert firm["tilt"] > stressed["tilt"]
    assert any("credit" in f for f in firm["factors"])
    # Macro is supplementary: missing rates/dollar data must NOT flag the reading stale.
    assert firm["stale"] is False


def test_equity_macro_missing_does_not_flag_stale(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_prices(monkeypatch, {"^GSPC": UP, "^VIX": _flat(12)})     # only primary signals present
    r = real_market_regime("US")
    assert r["stale"] is False and r["state"] == "risk-on"           # macro absent → still fine


def test_crypto_blends_fear_greed(monkeypatch):
    _bypass_cache(monkeypatch)
    _patch_prices(monkeypatch, {"BTC-USD": UP})
    from pipeline_core.services import market_sentiment
    monkeypatch.setattr(market_sentiment, "fear_greed",
                        lambda: {"value": 90, "classification": "Extreme Greed", "stale": False})
    r = real_market_regime("CRYPTO")
    assert r["state"] == "risk-on" and r["tilt"] > 0
    assert r["volatility"]["label"] == "Fear & Greed" and r["volatility"]["value"] == 90


def test_degrades_to_neutral_offline(monkeypatch):
    _bypass_cache(monkeypatch)
    from pipeline_core.services import market_data

    def boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(market_data, "get_price_history", boom)
    r = real_market_regime("US")
    assert r["tilt"] == 0.0 and r["stale"] is True and r["state"] == "neutral"


def test_pure_helpers():
    assert mr._trend(UP["Close"])[0] is True
    assert mr._trend(DOWN["Close"])[0] is False
    assert mr._trend(pd.Series([], dtype=float))[0] is None
    assert (mr._state(80), mr._state(50), mr._state(20)) == ("risk-on", "neutral", "risk-off")
    assert mr._vol_level(12) == "calm" and mr._vol_level(35) == "high"


def test_idea_cyclicality():
    assert mr.idea_cyclicality(["Trend", "Momentum"]) == 1.0        # pro-cyclical
    assert mr.idea_cyclicality(["Mean Reversion", "Contrarian"]) == -1.0   # counter-cyclical
    assert mr.idea_cyclicality(["Trend", "Mean Reversion"]) == 0.0  # mixed
    assert mr.idea_cyclicality([]) == 0.0 and mr.idea_cyclicality(["Unknown"]) == 0.0


# ── 2. horizon_ideas wiring (reorder + size haircut) ────────────────


def _risk_off():
    return {"market": "US", "state": "risk-off", "score": 20.0, "tilt": -0.6,
            "benchmark": {"label": "S&P 500", "trend": "down", "detail": "x"},
            "volatility": {"label": "VIX", "value": 30.0, "level": "high", "percentile": 95},
            "factors": [], "as_of": "2026-08-01T00:00:00Z", "stale": False}


def _two_strategy_latest(sig):
    """A backtest payload with one Trend (pro-cyclical) and one Mean-Reversion (counter-cyclical)
    strategy, each flagging a BUY on its own ticker with identical metrics — so cyclicality is the
    only thing that can separate them."""
    metrics = {"sharpe": 1.0, "win_rate": 55, "expectancy_pct": 2.0}
    def strat(sid, name, cat, tk):
        return {"id": sid, "name": name, "category": cat, "metrics": metrics,
                "signals": {"as_of": sig, "sell": [], "holding": [],
                            "buy": [{"ticker": tk, "price": 100.0, "signal_date": sig}]}}
    return {"run_at": "2026-07-26T00:00:00Z", "market": "US", "index": "IDX",
            "benchmark_label": "S&P 500", "years": 10, "disclaimer": "d",
            "strategies": [strat("golden-cross", "Golden Cross", "Trend", "TREND"),
                           strat("rsi-oversold-reversal", "RSI Oversold", "Mean Reversion", "MREV")]}


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "TRACKED_FILE", tmp_path / "tracked.json")
    runner._save_tracked([])
    monkeypatch.setattr(runner, "strategy_horizon", lambda sdef: "long")   # both ideas → one bucket
    sig = datetime.now(timezone.utc).date().isoformat()
    monkeypatch.setattr(runner, "load_latest", lambda market="IN": _two_strategy_latest(sig))
    return monkeypatch


def test_risk_off_reorders_by_cyclicality(seeded):
    seeded.setattr(mr, "market_regime", lambda market=None: _risk_off())
    cands = runner.horizon_ideas(market="US")["buckets"]["long"]["candidates"]
    order = [c["ticker"] for c in cands]
    assert order.index("MREV") < order.index("TREND")   # counter-cyclical long rises in risk-off
    fit = {c["ticker"]: c["regime_fit"] for c in cands}
    assert fit["MREV"] == "tailwind" and fit["TREND"] == "headwind"


def test_risk_off_trims_size(seeded):
    base = runner.horizon_ideas(market="US")["buckets"]["long"]          # conftest stub → neutral
    assert "regime_size_factor" not in base["sizing"]
    for c in base["candidates"]:
        assert "regime_size_factor" not in c and "regime_fit" not in c

    seeded.setattr(mr, "market_regime", lambda market=None: _risk_off())
    off = runner.horizon_ideas(market="US")["buckets"]["long"]
    assert off["sizing"]["regime_size_factor"] < 1.0
    assert all(c["regime_size_factor"] < 1.0 for c in off["candidates"])
    assert off["sizing"]["total_risk_pct"] <= base["sizing"]["total_risk_pct"]


def test_regime_block_present_and_neutral_by_default(seeded):
    res = runner.horizon_ideas(market="US")
    assert res["regime"] is not None and res["regime"]["state"] == "neutral"   # from the conftest stub


def test_gate_disabled_is_noop(seeded):
    seeded.setattr(runner.settings, "regime_gate_enabled", False)
    res = runner.horizon_ideas(market="US")
    assert res["regime"] is None
    for c in res["buckets"]["long"]["candidates"]:
        assert "regime_fit" not in c and "regime_size_factor" not in c
