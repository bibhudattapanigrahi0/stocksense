"""Composite idea-ranking factors + the pool re-rank. Pure/offline."""

import pytest

from pipeline_core.backtest import idea_ranking as ir
from pipeline_core.backtest import runner
# Capture the real news_sentiment / sector_rs at import (before the conftest autouse fixture stubs them).
from pipeline_core.services.market_sentiment import news_sentiment as _real_news_sentiment
from pipeline_core.services.sector import sector_rs as _real_sector_rs


# ── Factor math ─────────────────────────────────────────────────────

def test_earnings_factor_ramps_and_gates():
    assert ir.earnings_factor(0) == -1.0                 # earnings today → full penalty
    assert ir.earnings_factor(7) == 0.0                  # at the window edge → no penalty
    assert round(ir.earnings_factor(3), 3) == -0.571     # partway → partial penalty
    assert ir.earnings_factor(10) is None                # beyond the window → neutral
    assert ir.earnings_factor(None) is None
    assert ir.earnings_factor(-1) is None                # stale/past → neutral


def test_news_and_quality_factors_symmetric():
    assert ir.news_factor(50) == 0.0
    assert ir.news_factor(100) == 1.0 and ir.news_factor(0) == -1.0
    assert ir.news_factor(None) is None
    assert ir.quality_factor(75) == 0.5 and ir.quality_factor(25) == -0.5
    assert ir.quality_factor(None) is None


def test_sector_factor_squashes():
    assert ir.sector_factor(0) == 0.0
    assert 0.7 < ir.sector_factor(10) < 0.8               # +10pp RS → strong but bounded <1
    assert -1.0 <= ir.sector_factor(-100) <= -0.99        # saturates at the −1 bound
    assert ir.sector_factor(None) is None


def test_quality_score_from_fundamentals():
    strong = {"roe": 0.30, "debt_to_equity": 20.0, "profit_margins": 0.25}   # high ROE, low debt, fat margin
    weak = {"roe": 0.02, "debt_to_equity": 250.0, "profit_margins": -0.05}
    assert ir.quality_score(strong) > 80
    assert ir.quality_score(weak) < 20
    assert ir.quality_score({}) is None and ir.quality_score(None) is None    # no fundamentals → neutral
    assert ir.quality_factor(ir.quality_score(strong)) > 0.5                  # strong → positive tilt


def test_composite_multiplier_neutral_is_one():
    assert ir.composite_multiplier({}) == 1.0
    assert ir.composite_multiplier({"earnings": None, "news": None}) == 1.0


def test_composite_multiplier_applies_weight(monkeypatch):
    monkeypatch.setattr(ir.settings, "rank_w_earnings", 0.4)
    monkeypatch.setattr(ir.settings, "rank_w_news", 0.0)         # disabled → ignored even if present
    m = ir.composite_multiplier({"earnings": -1.0, "news": 1.0})
    assert m == pytest.approx(0.6)                                # 1 + 0.4*(-1); news weight 0


# ── Pool re-rank ────────────────────────────────────────────────────

def _cand(t, base, **extra):
    return {"ticker": t, "_base_score": base, "conviction": int(base),
            "avg_win_rate": 50, "avg_sharpe": 1.0, **extra}


def test_recompose_neutral_preserves_base_order():
    bucket = {"candidates": [_cand("A", 5), _cand("B", 4)],
              "_reserve": [_cand("C", 3), _cand("D", 2)]}
    runner._recompose_bucket(bucket, tilt=0.0, max_per_bucket=2)
    assert [c["ticker"] for c in bucket["candidates"]] == ["A", "B"]   # unchanged
    assert "_reserve" not in bucket                                    # consumed
    assert all("_base_score" not in c for c in bucket["candidates"])   # internal field cleaned


def test_recompose_promotes_reserve_over_earnings_risk(monkeypatch):
    monkeypatch.setattr(ir.settings, "rank_w_earnings", 0.5)
    # A is the top base idea but reports earnings tomorrow (factor −1 → ×0.5 → score 2.5);
    # reserve C (base 3, no earnings) now outranks it and is promoted into the shown top-2.
    bucket = {"candidates": [_cand("A", 5, earnings_factor=ir.earnings_factor(0)), _cand("B", 4)],
              "_reserve": [_cand("C", 3), _cand("D", 2)]}
    runner._recompose_bucket(bucket, tilt=0.0, max_per_bucket=2)
    shown = [c["ticker"] for c in bucket["candidates"]]
    assert shown == ["B", "C"]          # A (2.5) drops out; B (4) and C (3) shown


def test_quality_gate_drops_conflicted_ideas(monkeypatch):
    monkeypatch.setattr(ir.settings, "rank_min_multiplier", 0.80)
    monkeypatch.setattr(ir.settings, "rank_w_news", 0.20)
    monkeypatch.setattr(ir.settings, "rank_w_sector", 0.20)
    # A: bearish news (−1) AND lagging sector (−1) → mult 0.8×0.8 = 0.64 < 0.80 → gated out.
    # B: clean → mult 1.0 → kept. Quality over quantity: the conflicted idea is dropped, not shown.
    bucket = {"candidates": [_cand("A", 5, news_factor=-1.0, sector_factor=-1.0), _cand("B", 4)],
              "_reserve": []}
    runner._recompose_bucket(bucket, tilt=0.0, max_per_bucket=10)
    assert [c["ticker"] for c in bucket["candidates"]] == ["B"]
    assert bucket["quality_cut"] == 1


def test_quality_gate_disabled_keeps_all(monkeypatch):
    monkeypatch.setattr(ir.settings, "rank_min_multiplier", 0.0)
    monkeypatch.setattr(ir.settings, "rank_w_news", 0.20)
    bucket = {"candidates": [_cand("A", 5, news_factor=-1.0), _cand("B", 4)], "_reserve": []}
    runner._recompose_bucket(bucket, tilt=0.0, max_per_bucket=10)
    assert {c["ticker"] for c in bucket["candidates"]} == {"A", "B"}   # gate off → nothing dropped
    assert bucket["quality_cut"] == 0


def test_recompose_dedups_correlated_and_promotes_independent(monkeypatch):
    monkeypatch.setattr(ir.settings, "rank_min_multiplier", 0.0)   # isolate de-dup from the quality gate
    # A & B are correlated (cluster 0); C is independent (cluster 1). A(5) outranks B(4), so B is
    # collapsed as a duplicate and independent reserve C(3) is promoted into the shown top-2 — the
    # board shows independent bets, not the same trade twice.
    bucket = {"candidates": [_cand("A", 5), _cand("B", 4)], "_reserve": [_cand("C", 3)]}
    clusters = {"A": 0, "B": 0, "C": 1}
    runner._recompose_bucket(bucket, tilt=0.0, max_per_bucket=2, clusters=clusters)
    assert [c["ticker"] for c in bucket["candidates"]] == ["A", "C"]
    assert bucket["dedup"]["dropped"] == 1 and bucket["dedup"]["collapsed_clusters"] == 1
    assert bucket["candidates"][0]["cluster_peers"] == 2      # A stands in for the 2-name cluster


def test_recompose_no_clusters_is_noop():
    # No cluster map (the default / offline) → nothing collapsed, base order preserved, dedup summary
    # present but empty. Keeps the board byte-identical to pre-de-dup when correlation data is absent.
    bucket = {"candidates": [_cand("A", 5), _cand("B", 4)], "_reserve": [_cand("C", 3)]}
    runner._recompose_bucket(bucket, tilt=0.0, max_per_bucket=2)   # clusters defaults to None
    assert [c["ticker"] for c in bucket["candidates"]] == ["A", "B"]
    assert bucket["dedup"]["dropped"] == 0


def test_recompose_news_reorders(monkeypatch):
    monkeypatch.setattr(ir.settings, "rank_w_news", 0.3)
    monkeypatch.setattr(ir.settings, "rank_min_multiplier", 0.0)   # isolate reordering from the gate
    # A (base 5) has bearish news (score 0 → factor −1 → ×0.7 = 3.5); neutral B (base 4) leads it.
    bucket = {"candidates": [_cand("A", 5, news_factor=ir.news_factor(0)), _cand("B", 4)], "_reserve": []}
    runner._recompose_bucket(bucket, tilt=0.0, max_per_bucket=2)
    assert [c["ticker"] for c in bucket["candidates"]] == ["B", "A"]


def test_news_sentiment_caches_success_and_falls_back(monkeypatch):
    import asyncio
    from pipeline_core.agents import sentiment as sent_mod
    from pipeline_core.services import market_sentiment as ms

    async def bullish(ticker, name=None, market="IN"):
        return {"score": 72.0, "consensus": "Bullish", "method": "lexicon"}

    stored = {}
    monkeypatch.setattr(ms._news_result_cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(ms._news_result_cache, "set", lambda k, v, **kw: stored.__setitem__(k, v))
    monkeypatch.setattr(sent_mod, "analyze_ticker_sentiment", bullish)
    out = asyncio.run(_real_news_sentiment("AAPL", None, "US"))
    assert out["score"] == 72.0 and out["consensus"] == "Bullish" and stored   # cached the success

    async def boom(*a, **k):
        raise RuntimeError("blocked")

    monkeypatch.setattr(sent_mod, "analyze_ticker_sentiment", boom)
    monkeypatch.setattr(ms._news_result_cache, "set", lambda *a, **k: None)
    neutral = asyncio.run(_real_news_sentiment("X", None, "IN"))
    assert neutral["score"] == 50.0 and neutral["method"] == "unavailable"     # graceful


def test_sector_rs_relative_return(monkeypatch):
    import pandas as pd
    from pipeline_core.services import market_data, sector as sec

    def fake_hist(symbol, period="6mo", interval="1d", market="IN", **kw):
        ret = 0.20 if symbol == "XLK" else 0.08          # XLK +20%, benchmark (^GSPC) +8%
        vals = [100.0] * 16 + [100.0 * (1 + ret * i / 63) for i in range(64)]
        return pd.DataFrame({"Close": vals})

    monkeypatch.setattr(market_data, "get_price_history", fake_hist)
    monkeypatch.setattr(sec._cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(sec._cache, "set", lambda *a, **k: None)
    assert _real_sector_rs("Technology", "US") == pytest.approx(12.0)   # 20% − 8%
    assert _real_sector_rs("Nonexistent Sector", "US") is None          # unmapped sector → neutral
    assert _real_sector_rs("Technology", "IN") is None                  # no IN sector map yet
