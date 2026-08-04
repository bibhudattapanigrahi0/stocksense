"""Sentiment-aware statistical curation of intraday Trade Ideas — the selection layer.

Covers the pure math (Wilson lower bound, sentiment alignment, composite score, curation),
the Fear & Greed gauge (parse + neutral fallback), and the end-to-end `intraday_ideas`
shortlist (present, capped, sized; sentiment soft-penalty; sentiment=False neutral path).
Network-free: OHLCV is synthetic and both sentiment sources are monkeypatched."""

import numpy as np
import pandas as pd
import pytest

from pipeline_core.bots import backtest as bt
from pipeline_core.bots import ideas as ideas_mod
from pipeline_core.bots import selection as sel
from pipeline_core.services import market_sentiment as ms
# Bound at import → the REAL implementation, unaffected by the conftest fixture that stubs the
# module attribute `ms.fear_greed` (so we can test the live-fetch code path directly).
from pipeline_core.services.market_sentiment import fear_greed as real_fear_greed


def frame(closes, vols=None):
    closes = np.asarray(closes, float)
    n = len(closes)
    idx = pd.date_range("2026-07-27", periods=n, freq="15min")
    return pd.DataFrame({
        "Open": closes, "High": closes * 1.006, "Low": closes * 0.994, "Close": closes,
        "Volume": vols if vols is not None else np.full(n, 1e6),
    }, index=idx)


UP = frame(np.cumprod(1 + np.concatenate([[0], np.full(159, 0.004)])) * 100)
DOWN = frame(np.cumprod(1 + np.concatenate([[0], np.full(159, -0.004)])) * 150)


# ── 1. Confidence: Wilson lower bound ───────────────────────────────


def test_wilson_penalizes_small_samples():
    small = sel.wilson_lower_bound(60, 10)
    large = sel.wilson_lower_bound(60, 500)
    assert small < large <= 60                      # more data → trust closer to the observed rate
    assert sel.wilson_lower_bound(70, 100) > sel.wilson_lower_bound(50, 100)   # monotonic in win %


def test_wilson_unproven_prior():
    assert sel.wilson_lower_bound(60, None) == sel.UNPROVEN_WIN_PCT
    assert sel.wilson_lower_bound(60, 0) == sel.UNPROVEN_WIN_PCT
    assert sel.wilson_lower_bound(None, 100) == sel.UNPROVEN_WIN_PCT


# ── 2. Sentiment alignment ──────────────────────────────────────────


def test_sentiment_alignment_direction():
    assert sel.sentiment_alignment("long", 90, 50) > 0      # bullish news supports a long
    assert sel.sentiment_alignment("long", 10, 50) < 0      # bearish news contradicts a long
    assert sel.sentiment_alignment("short", 10, 50) > 0     # bearish news supports a short
    assert sel.sentiment_alignment("short", 90, 50) < 0
    assert sel.sentiment_alignment("long", None, None) == 0.0   # missing inputs → neutral
    assert sel.sentiment_alignment("long", 50, 50) == 0.0


def test_fng_contrarian_at_extremes():
    assert sel.sentiment_alignment("long", 50, 95) < 0      # extreme greed fades a fresh long
    assert sel.sentiment_alignment("short", 50, 95) > 0     # …and favours a short
    assert sel.sentiment_alignment("long", 50, 5) > 0       # extreme fear favours a long
    assert sel.sentiment_alignment("short", 50, 5) < 0
    assert sel.sentiment_alignment("long", 50, 70) > 0      # normal greed → momentum supports longs


# ── 3. Composite score (soft penalty) ───────────────────────────────


def test_composite_soft_penalty_and_bounds():
    neutral = sel.composite_score(0.7, 0.6, 0.8, 0.0)
    supportive = sel.composite_score(0.7, 0.6, 0.8, 1.0)
    contra = sel.composite_score(0.7, 0.6, 0.8, -1.0)
    assert 0.0 < contra < neutral < supportive <= 1.0   # sentiment shifts, never zeroes (soft)
    assert sel.composite_score(1.0, 1.0, 1.0, 1.0) == 1.0   # clamped to 1


def test_score_candidate_shape_and_penalty():
    base = dict(side="long", conviction=2, quality=0.4, r_multiple=2.0, est_win_pct=58, trades=120)
    neu = sel.score_candidate(**base, news_score=50, fng_value=50)
    bear = sel.score_candidate(**base, news_score=5, fng_value=50)
    assert set(neu) >= {"conf_win_pct", "alignment", "selection_score", "terms"}
    assert neu["conf_win_pct"] < 58                          # Wilson shrinks the observed rate
    assert bear["selection_score"] < neu["selection_score"]  # contradicting news down-ranks


# ── 4. Curation: rank → floor → cap ─────────────────────────────────


def test_curate_caps_and_floors():
    cands = [{"selection_score": s, "side": "long", "conf_win_pct": 50, "conviction": 1, "r_multiple": 1}
             for s in (0.9, 0.8, 0.7, 0.3, 0.1)]
    kept, diag = sel.curate(cands, top_k=3, min_score=0.5)
    assert [c["selection_score"] for c in kept] == [0.9, 0.8, 0.7]   # ranked desc, capped at top_k
    assert diag == {"considered": 5, "kept": 3, "cut": 2, "top_k": 3, "min_score": 0.5}
    kept2, _ = sel.curate(cands, top_k=10, min_score=0.5)
    assert len(kept2) == 3                                   # min_score floor drops the weak two


# ── 5. Fear & Greed gauge ───────────────────────────────────────────


def test_classify_buckets():
    assert ms._classify(10) == "Extreme Fear"
    assert ms._classify(35) == "Fear"
    assert ms._classify(50) == "Neutral"
    assert ms._classify(65) == "Greed"
    assert ms._classify(90) == "Extreme Greed"


def test_fear_greed_parses(monkeypatch):
    monkeypatch.setattr(ms._cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(ms._cache, "set", lambda *a, **k: None)
    import requests

    class R:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"value": "72", "value_classification": "Greed", "timestamp": "1700000000"}]}

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    out = real_fear_greed()
    assert out["value"] == 72 and out["classification"] == "Greed" and out["stale"] is False


def test_fear_greed_fallback_neutral(monkeypatch):
    monkeypatch.setattr(ms._cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(ms._cache, "set", lambda *a, **k: None)
    import requests

    def boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(requests, "get", boom)
    out = real_fear_greed()
    assert out["value"] == 50 and out["stale"] is True       # degrades, never raises


# ── 6. End-to-end shortlist ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_shortlist_present_capped_and_sized(monkeypatch):
    monkeypatch.setattr(ideas_mod, "get_universe_tickers", lambda m, u: ["AAA"])
    monkeypatch.setattr(ideas_mod.crypto_data, "get_klines", lambda s, tf, bars, closed_only=True: DOWN.copy())
    monkeypatch.setattr(bt, "load_edge", lambda: None)
    res = await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50", top_k=3)

    assert "shortlist" in res and 1 <= len(res["shortlist"]) <= 3
    assert res["selection"]["top_k"] == 3 and "weights" in res["selection"]
    assert res["fear_greed"] is not None                     # conftest stub → neutral reading
    assert res["shortlist_sizing"]["sized"] == len(res["shortlist"])
    for i in res["shortlist"]:
        assert i["selection_score"] is not None and i["conf_win_pct"] is not None
        assert i["sentiment"]["stance"] in ("supportive", "contradicting", "neutral")
        assert i["why_selected"]


@pytest.mark.asyncio
async def test_shortlist_soft_penalty_downranks_contradicting_news(monkeypatch):
    # Two coins with IDENTICAL technicals (same frame) → they differ only by news, so the coin
    # whose news supports the long must outrank the one whose news contradicts it.
    monkeypatch.setattr(ideas_mod, "get_universe_tickers", lambda m, u: ["BULL", "BEAR"])
    monkeypatch.setattr(ideas_mod.crypto_data, "get_klines", lambda s, tf, bars, closed_only=True: UP.copy())
    monkeypatch.setattr(ideas_mod.dedup, "cluster_symbols", lambda frames: {})   # keep both (identical → would cluster)
    monkeypatch.setattr(bt, "load_edge", lambda: None)
    monkeypatch.setattr(ms, "fear_greed", lambda: {"value": 50, "classification": "Neutral", "stale": False})

    async def news(tk, name=None):
        return {"BULL": {"score": 90, "consensus": "Bullish"},
                "BEAR": {"score": 10, "consensus": "Bearish"}}[tk]

    monkeypatch.setattr(ms, "coin_news", news)
    res = await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50", top_k=10)

    longs = {i["ticker"]: i for i in res["shortlist"] if i["side"] == "long"}
    assert "BULL" in longs and "BEAR" in longs
    assert longs["BULL"]["sentiment"]["stance"] == "supportive"
    assert longs["BEAR"]["sentiment"]["stance"] == "contradicting"
    assert longs["BULL"]["selection_score"] > longs["BEAR"]["selection_score"]
    order = [i["ticker"] for i in res["shortlist"] if i["side"] == "long"]
    assert order.index("BULL") < order.index("BEAR")         # supportive ranks ahead


@pytest.mark.asyncio
async def test_sentiment_off_is_neutral(monkeypatch):
    monkeypatch.setattr(ideas_mod, "get_universe_tickers", lambda m, u: ["AAA"])
    monkeypatch.setattr(ideas_mod.crypto_data, "get_klines", lambda s, tf, bars, closed_only=True: DOWN.copy())
    monkeypatch.setattr(bt, "load_edge", lambda: None)

    # Even a strong bearish/extreme reading must be ignored when sentiment is disabled.
    async def news(tk, name=None):
        return {"score": 5, "consensus": "Bearish"}

    monkeypatch.setattr(ms, "coin_news", news)
    monkeypatch.setattr(ms, "fear_greed", lambda: {"value": 95, "classification": "Extreme Greed", "stale": False})
    res = await ideas_mod.intraday_ideas(timeframe="15m", universe="Top 50", sentiment=False)

    assert res["selection"]["sentiment"] is False and res["fear_greed"] is None
    for i in res["shortlist"]:
        assert i["sentiment"]["alignment"] == 0.0 and i["sentiment"]["news_score"] is None


@pytest.fixture
def anyio_backend():
    return "asyncio"
