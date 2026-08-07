"""Multi-bagger screen — pillar math, candidate scoring, and the staged screen. Pure/offline.

Mirrors tests/test_idea_ranking.py: the scoring is a pure function of numbers a profile already
carries, so most tests need no network; the `screen_multibaggers` tests stub the two I/O calls
(`fetch_batch_info` + the deep stage) so the suite stays hermetic.
"""

from __future__ import annotations

import pytest

from pipeline_core import multibagger as mb
from pipeline_core.config import settings
from tests.conftest import make_stock


# ── Pillar sub-scores (each 0..1, or None when data is absent) ──────

def test_growth_pillar_rewards_fast_growth():
    fast = mb.growth_score({"revenue_growth": 0.30, "earnings_growth": 0.35})
    slow = mb.growth_score({"revenue_growth": 0.02, "earnings_growth": 0.0})
    assert fast > 0.9 and slow < 0.1
    assert mb.growth_score({}) is None                       # no growth metrics → neutral/absent


def test_size_pillar_favours_smaller_caps():
    # Two otherwise-identical names: the smaller cap must score higher on runway.
    small = mb.size_score({"market_cap": 1e9}, "US")
    big = mb.size_score({"market_cap": 5e11}, "US")
    assert small > big
    assert small == pytest.approx(1.0)                       # at/below the small anchor → max
    assert mb.size_score({"market_cap": 0}, "US") is None
    # Per-market anchors: the INR scale reads a ₹1,000 Cr name as small, not mega.
    assert mb.size_score({"market_cap": 1e10}, "IN") > 0.9


def test_value_pillar_pegs_first_then_pe():
    assert mb.value_score({"peg_ratio": 0.5}) == pytest.approx(1.0)
    assert mb.value_score({"peg_ratio": 2.5}) == pytest.approx(0.0)
    assert mb.value_score({"peg_ratio": -1}) == 0.2          # loss-maker → low, not absent
    assert mb.value_score({"pe_ratio": 20}) > 0.5            # falls back to a P/E band
    assert mb.value_score({}) is None


def test_balance_pillar_handles_yfinance_percent_debt():
    # yfinance reports debt/equity as a percent (25 = 0.25×) — low leverage should score well.
    strong = mb.balance_score({"debt_to_equity": 15.0, "free_cash_flow": 1e9})
    weak = mb.balance_score({"debt_to_equity": 250.0, "free_cash_flow": -1e9})
    assert strong > 0.8 and weak < 0.2


def test_ownership_pillar_blends_insiders_and_promoter():
    high = mb.ownership_score({"held_pct_insiders": 0.35, "promoter_holding_pct": 62.0, "pledge_pct": 0.0})
    low = mb.ownership_score({"held_pct_insiders": 0.01, "promoter_holding_pct": 20.0, "pledge_pct": 40.0})
    assert high > 0.9 and low < 0.1
    assert mb.ownership_score({}) is None


# ── Composite candidate scoring ─────────────────────────────────────

def _strong_smallcap(**over):
    return make_stock(ticker="SMALLCO", market_cap=2e9, market_cap_cr=200.0,
                      week_52_low=80.0, week_52_high=140.0, price=100.0,
                      revenue_growth=0.28, earnings_growth=0.32, roe=0.28, debt_to_equity=15.0,
                      profit_margins=0.22, peg_ratio=0.9, held_pct_insiders=0.35, **over)


def test_strong_compounder_outranks_weak_megacap():
    strong = mb.score_candidate(_strong_smallcap(), "US")
    weak = mb.score_candidate(make_stock(
        ticker="MEGACO", market_cap=8e11, week_52_low=100.0, week_52_high=200.0, price=195.0,
        revenue_growth=0.02, earnings_growth=-0.05, roe=0.05, debt_to_equity=250.0,
        profit_margins=0.03, peg_ratio=3.2, held_pct_insiders=0.01), "US")
    assert strong["score"] > weak["score"]
    assert strong["score"] > 75 and weak["score"] < 25
    # Pillars are surfaced 0..100 for the UI; the strong name hits growth/size hard.
    assert strong["pillars"]["growth"] > 80 and strong["pillars"]["size"] > 80


def test_reasons_and_concerns_populate():
    strong = mb.score_candidate(_strong_smallcap(), "US")
    weak = mb.score_candidate(make_stock(
        ticker="BADCO", market_cap=9e11, revenue_growth=0.0, earnings_growth=-0.1,
        roe=0.03, debt_to_equity=300.0, profit_margins=0.01, peg_ratio=4.0,
        held_pct_insiders=0.0), "US")
    assert strong["reasons"] and not strong["concerns"]
    assert weak["concerns"]
    assert any("growth" in r.lower() for r in strong["reasons"])


def test_coverage_gate_drops_thin_profiles():
    # A near-empty profile (only a name/ticker) can't be judged → dropped, not flattered.
    assert mb.score_candidate({"ticker": "X", "name": "X"}, "US") is None
    # Missing fields renormalise: a name with only size + quality present still scores.
    partial = mb.score_candidate({"ticker": "P", "name": "P", "market_cap": 1e9,
                                  "roe": 0.25, "profit_margins": 0.2, "debt_to_equity": 20.0}, "US")
    assert partial is not None and 0 <= partial["score"] <= 100
    assert partial["coverage"] < 1.0                          # not every pillar had data


def test_technical_overlay_is_bounded_and_cannot_flip_fundamentals():
    base = _strong_smallcap()   # already priced early in its 52-week range (100 in 80–140)
    # A weak name with the maximum timing tailwind must still not overtake the strong one.
    weak = make_stock(ticker="WEAK", market_cap=8e11, revenue_growth=0.01, earnings_growth=0.0,
                      roe=0.04, debt_to_equity=260.0, profit_margins=0.02, peg_ratio=3.5,
                      held_pct_insiders=0.0, price=85.0, week_52_low=80.0, week_52_high=200.0)
    strong_c = mb.score_candidate(base, "US", buy_signal=False)
    weak_c = mb.score_candidate(weak, "US", buy_signal=True)
    assert strong_c["score"] > weak_c["score"]
    # Overlay itself stays in [-1, 1].
    assert -1.0 <= mb.technical_overlay(base, True) <= 1.0
    assert -1.0 <= mb.technical_overlay(weak, False) <= 1.0
    # The buy signal is surfaced on the candidate.
    assert weak_c["buy_signal_now"] is True


def test_buy_signal_adds_a_reason():
    with_sig = mb.score_candidate(_strong_smallcap(), "US", buy_signal=True)
    assert any("buy" in r.lower() for r in with_sig["reasons"])


# ── The staged screen (I/O stubbed) ─────────────────────────────────

async def test_price_only_market_returns_note_without_network():
    out = await mb.screen_multibaggers("METALS", persist=False)
    assert out["candidates"] == [] and out["count"] == 0
    assert "note" in out and "equity" in out["note"].lower()


async def test_screen_ranks_and_caps(monkeypatch):
    universe = [f"T{i}" for i in range(6)]
    # Six synthetic names with descending fundamental quality.
    infos = []
    for i in range(6):
        infos.append(make_stock(
            ticker=f"T{i}", market_cap=(1e9 + i * 2e11),
            revenue_growth=0.30 - i * 0.05, earnings_growth=0.32 - i * 0.05,
            roe=0.28 - i * 0.03, debt_to_equity=15.0 + i * 40, profit_margins=0.22 - i * 0.03,
            peg_ratio=0.9 + i * 0.4, held_pct_insiders=0.30 - i * 0.05))

    async def _fake_batch(symbols, progress_callback=None, market="IN"):
        if progress_callback:
            progress_callback(len(symbols), len(symbols))
        return infos

    async def _no_deep(ticker, market):
        return {}

    monkeypatch.setattr(mb, "_universe_for", lambda market: universe)
    monkeypatch.setattr(mb, "_current_buy_set", lambda market: set())
    monkeypatch.setattr(mb.market_data, "fetch_batch_info", _fake_batch)
    monkeypatch.setattr(mb, "_fetch_deep", _no_deep)

    out = await mb.screen_multibaggers("US", top_n=3, persist=False)
    assert out["count"] == 3                                  # capped at top_n
    scores = [c["score"] for c in out["candidates"]]
    assert scores == sorted(scores, reverse=True)             # ranked best-first
    assert out["candidates"][0]["ticker"] == "T0"            # the strongest name leads
    assert out["market"] == "US" and out["screened"] == 6


async def test_screen_applies_buy_signal_overlay(monkeypatch):
    info = make_stock(ticker="AAA", market_cap=2e9, revenue_growth=0.25, earnings_growth=0.25,
                      roe=0.25, debt_to_equity=20.0, profit_margins=0.2, peg_ratio=1.0,
                      held_pct_insiders=0.2, price=100.0, week_52_low=90.0, week_52_high=200.0)

    async def _fake_batch(symbols, progress_callback=None, market="IN"):
        return [info]

    async def _no_deep(ticker, market):
        return {}

    monkeypatch.setattr(mb, "_universe_for", lambda market: ["AAA"])
    monkeypatch.setattr(mb, "_current_buy_set", lambda market: {"AAA"})   # AAA has a live buy
    monkeypatch.setattr(mb.market_data, "fetch_batch_info", _fake_batch)
    monkeypatch.setattr(mb, "_fetch_deep", _no_deep)

    out = await mb.screen_multibaggers("US", top_n=5, persist=False)
    assert out["candidates"][0]["buy_signal_now"] is True


def test_pillar_weights_come_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "multibagger_w_size", 0.99)
    assert mb._pillar_weights()["size"] == 0.99


# ── Tracking (bagger multiple / milestones / thesis drift) ─────────

def _stub_screen(monkeypatch, tmp_path, candidates):
    """Isolate the tracker file to tmp_path and stub the persisted screen it snapshots from."""
    monkeypatch.setattr(mb, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(mb, "load_latest_multibaggers",
                        lambda market="IN": {"run_at": "2026-06-01T00:00:00+00:00",
                                             "market": "US", "candidates": candidates})


def test_milestone_label():
    assert mb._milestone_label(2.5) == "2× (double bagger)"
    assert mb._milestone_label(11.0) == "10× 🚀"
    assert mb._milestone_label(1.2) is None
    assert mb._milestone_label(None) is None


def test_track_is_idempotent_and_supports_single_ticker(monkeypatch, tmp_path):
    cands = [{"ticker": "AAA", "name": "Alpha", "price": 100.0, "score": 80.0, "pillars": {}},
             {"ticker": "BBB", "name": "Beta", "price": 50.0, "score": 70.0, "pillars": {}}]
    _stub_screen(monkeypatch, tmp_path, cands)

    assert mb.track_multibaggers("US") == {"added": 2, "total": 2}
    assert mb.track_multibaggers("US") == {"added": 0, "total": 2}       # idempotent
    # Entry snapshot is captured.
    rec = next(t for t in mb._load_tracked("US") if t["ticker"] == "AAA")
    assert rec["entry_price"] == 100.0 and rec["entry_score"] == 80.0 and rec["entry_date"] == "2026-06-01"


def test_track_single_ticker_only(monkeypatch, tmp_path):
    cands = [{"ticker": "AAA", "price": 100.0, "score": 80.0}, {"ticker": "BBB", "price": 50.0, "score": 70.0}]
    _stub_screen(monkeypatch, tmp_path, cands)
    assert mb.track_multibaggers("US", ticker="BBB")["added"] == 1
    assert [t["ticker"] for t in mb._load_tracked("US")] == ["BBB"]
    # Unknown ticker → nothing added, with a note.
    r = mb.track_multibaggers("US", ticker="ZZZ")
    assert r["added"] == 0 and "not in the current" in r["note"]


def test_track_without_a_screen_returns_note(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(mb, "load_latest_multibaggers", lambda market="IN": None)
    r = mb.track_multibaggers("US")
    assert r["added"] == 0 and "run one first" in r["note"]


def test_untrack_one_and_all(monkeypatch, tmp_path):
    cands = [{"ticker": "AAA", "price": 100.0, "score": 80.0}, {"ticker": "BBB", "price": 50.0, "score": 70.0}]
    _stub_screen(monkeypatch, tmp_path, cands)
    mb.track_multibaggers("US")
    assert mb.untrack_multibaggers("US", ticker="AAA") == {"removed": 1}
    assert mb.untrack_multibaggers("US", ticker="ZZZ") == {"removed": 0}   # unknown → no-op
    assert mb.untrack_multibaggers("US") == {"removed": 1}                 # clear the rest
    assert mb._load_tracked("US") == []


async def test_tracked_status_computes_multiple_milestone_and_drift(monkeypatch, tmp_path):
    cands = [{"ticker": "AAA", "name": "Alpha", "price": 100.0, "score": 80.0, "pillars": {}},
             {"ticker": "BBB", "name": "Beta", "price": 50.0, "score": 60.0, "pillars": {}}]
    _stub_screen(monkeypatch, tmp_path, cands)
    mb.track_multibaggers("US")

    quotes = {"AAA": 250.0, "BBB": 45.0}    # AAA 2.5× (doubled), BBB down 10%

    async def _q(sym, market="IN", max_age=None):
        return quotes.get(sym)

    async def _info(sym, market="IN"):       # a solid profile → re-scores well
        return {"ticker": sym, "name": sym, "market_cap": 2e9, "roe": 0.25, "profit_margins": 0.2,
                "debt_to_equity": 20.0, "revenue_growth": 0.2, "earnings_growth": 0.2, "peg_ratio": 1.0}

    async def _deep(sym, market):
        return {}

    async def _no_hist(sym, period="1y", interval="1d", market="IN", **kw):
        return None

    monkeypatch.setattr(mb.market_data, "aget_quote", _q)
    monkeypatch.setattr(mb.market_data, "aget_stock_info", _info)
    monkeypatch.setattr(mb, "_fetch_deep", _deep)
    monkeypatch.setattr(mb.market_data, "aget_price_history", _no_hist)

    out = await mb.tracked_multibaggers_status("US")
    by = {r["ticker"]: r for r in out["tracked"]}
    assert by["AAA"]["multiple"] == 2.5 and by["AAA"]["return_pct"] == 150.0
    assert by["AAA"]["milestone"] == "2× (double bagger)"
    assert by["BBB"]["multiple"] == 0.9 and by["BBB"]["milestone"] is None
    # Ranked by multiple (best first) and re-scored (drift present).
    assert out["tracked"][0]["ticker"] == "AAA"
    assert by["AAA"]["current_score"] is not None and by["AAA"]["score_drift"] is not None
    # Scoreboard tallies the milestone.
    sb = out["scoreboard"]
    assert sb["count"] == 2 and sb["best_multiple"] == 2.5 and sb["milestones"]["2"] == 1
    assert out["curve"] == []                # no history stubbed → curve hides


async def test_tracked_status_empty_when_nothing_tracked(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "RESULTS_DIR", tmp_path)
    out = await mb.tracked_multibaggers_status("US")
    assert out["count"] == 0 and out["tracked"] == [] and out["curve"] == []
