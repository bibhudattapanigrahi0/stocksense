"""Live board pricing — enrich_ideas_live adds a live price, % move since signal, buy-zone
status, and recomputed 'live levels' without touching the generated plan. Network-free."""

import asyncio

from pipeline_core.backtest import runner


def _ideas():
    return {"market": "IN", "horizons": ["short"], "buckets": {"short": {"candidates": [
        {"ticker": "A", "price": 100.0, "buy_low": 98.0, "buy_high": 103.0,
         "stop_price": 94.0, "target_price": 118.0, "stop_pct": 0.06, "target_pct": 0.18,
         "risk_reward": 3.0},
    ]}}}


def test_enrich_adds_live_price_change_and_zone(monkeypatch):
    monkeypatch.setattr(runner, "get_quote", lambda tk, market="IN": 101.0)      # inside the buy zone
    out = asyncio.run(runner.enrich_ideas_live(_ideas(), "IN"))
    c = out["buckets"]["short"]["candidates"][0]
    assert c["live_price"] == 101.0 and c["zone"] == "in_zone"
    assert c["live_change_pct"] == 1.0                          # +1% vs signal price 100
    # Original plan untouched; live_levels recomputed from the live price.
    assert c["buy_low"] == 98.0 and c["target_price"] == 118.0
    assert c["live_levels"]["stop_price"] == 94.94             # 101 × (1 − 0.06)
    assert c["live_levels"]["risk_reward"] == 3.0
    assert out["live_priced_at"]


def test_enrich_handles_missing_quote(monkeypatch):
    monkeypatch.setattr(runner, "get_quote", lambda tk, market="IN": None)
    c = asyncio.run(runner.enrich_ideas_live(_ideas(), "IN"))["buckets"]["short"]["candidates"][0]
    assert c["live_price"] is None and c["zone"] is None and c["live_levels"] is None


def test_enrich_attaches_best_odds(monkeypatch):
    # The Best-Odds board rides along on the enriched payload (next to `basket`).
    monkeypatch.setattr(runner, "get_quote", lambda tk, market="IN": 101.0)
    out = asyncio.run(runner.enrich_ideas_live(_ideas(), "IN"))
    bo = out["best_odds"]
    assert bo["enabled"] is True and len(bo["items"]) == 1
    assert bo["items"][0]["ticker"] == "A" and bo["items"][0]["rank"] == 1


def test_zone_classification():
    c = {"buy_low": 98.0, "buy_high": 103.0, "stop_price": 94.0, "target_price": 118.0}
    assert runner._idea_zone(90.0, c) == "below_stop"
    assert runner._idea_zone(120.0, c) == "at_target"
    assert runner._idea_zone(96.0, c) == "below_zone"
    assert runner._idea_zone(110.0, c) == "above_zone"
    assert runner._idea_zone(100.0, c) == "in_zone"


def test_enrich_dedups_correlated_ideas(monkeypatch):
    """End-to-end: the live enrichment fetches daily history, clusters correlated names, and collapses
    them so the shown board is independent bets. A & B share an identical return path (→ one cluster);
    independent reserve C is promoted into B's place."""
    import numpy as np
    import pandas as pd

    monkeypatch.setattr(runner, "get_quote", lambda tk, market="IN": 100.0)
    idx = pd.date_range("2025-01-01", periods=120, freq="B")
    trend = np.linspace(100.0, 150.0, 120)                       # A and B: same returns (B just scaled)
    swing = 100.0 + 15.0 * np.sin(np.linspace(0.0, 14.0, 120))   # C: oscillating → uncorrelated
    frames = {"A": pd.DataFrame({"Close": trend}, index=idx),
              "B": pd.DataFrame({"Close": trend * 2.0}, index=idx),
              "C": pd.DataFrame({"Close": swing}, index=idx)}

    async def fake_hist(tk, period="6mo", interval="1d", market="IN", **kw):
        return frames.get(tk)

    monkeypatch.setattr(runner, "aget_price_history", fake_hist)

    ideas = {"market": "IN", "horizons": ["short"], "max_per_bucket": 2, "buckets": {"short": {
        "candidates": [{"ticker": "A", "price": 100.0, "conviction": 5, "_base_score": 5.0},
                       {"ticker": "B", "price": 100.0, "conviction": 4, "_base_score": 4.0}],
        "_reserve": [{"ticker": "C", "price": 100.0, "conviction": 3, "_base_score": 3.0}]}}}
    out = asyncio.run(runner.enrich_ideas_live(ideas, "IN"))
    bucket = out["buckets"]["short"]
    tickers = [c["ticker"] for c in bucket["candidates"]]
    assert "A" in tickers and "C" in tickers and "B" not in tickers   # B collapsed into A; C promoted
    assert bucket["dedup"]["dropped"] == 1


def test_enrich_no_history_skips_dedup(monkeypatch):
    """With no usable history (the offline default) clustering finds nothing, so de-dup is a no-op and
    both correlated-looking names stay — the board is identical to the pre-de-dup one."""
    monkeypatch.setattr(runner, "get_quote", lambda tk, market="IN": 100.0)
    # runner.aget_price_history is stubbed to return None by the autouse hermetic fixture.
    ideas = {"market": "IN", "horizons": ["short"], "max_per_bucket": 2, "buckets": {"short": {
        "candidates": [{"ticker": "A", "price": 100.0, "conviction": 5, "_base_score": 5.0},
                       {"ticker": "B", "price": 100.0, "conviction": 4, "_base_score": 4.0}],
        "_reserve": []}}}
    bucket = asyncio.run(runner.enrich_ideas_live(ideas, "IN"))["buckets"]["short"]
    assert [c["ticker"] for c in bucket["candidates"]] == ["A", "B"]
    assert bucket["dedup"]["dropped"] == 0


# ── Multi-pillar tiering wired end-to-end through enrich ─────────────

def test_enrich_tags_pillar_tier(monkeypatch):
    monkeypatch.setattr(runner, "get_quote", lambda tk, market="IN": 101.0)
    ideas = _ideas()
    ideas["buckets"]["short"]["candidates"][0]["conviction"] = 2       # technical pillar present
    out = asyncio.run(runner.enrich_ideas_live(ideas, "IN"))
    bucket = out["buckets"]["short"]
    c = bucket["candidates"][0]
    assert set(c["pillars"]) == set(runner.idea_ranking.PILLAR_NAMES)   # all families reported
    assert c["pillars"]["technical"] is True                           # conviction ≥ 1
    assert isinstance(c["pillar_count"], int) and c["tier"] in ("actionable", "watch")
    assert "actionable_count" in bucket and "watch_count" in bucket


def test_enrich_pillar_tiering_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(runner, "get_quote", lambda tk, market="IN": 101.0)
    monkeypatch.setattr(runner.settings, "pillar_tiering_enabled", False)
    ideas = _ideas()
    ideas["buckets"]["short"]["candidates"][0]["conviction"] = 2
    c = asyncio.run(runner.enrich_ideas_live(ideas, "IN"))["buckets"]["short"]["candidates"][0]
    assert "tier" not in c and "pillars" not in c                      # disabled → no tiering fields
