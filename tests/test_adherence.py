"""Adherence — 'system vs you' discipline scorecard. Pure core + the two-path orchestrator (mocked)."""

import asyncio

import pytest

from pipeline_core import adherence as ad


# ── Pure scoring core ───────────────────────────────────────────────

def test_exit_delta_verdicts():
    assert ad.exit_delta(100, 105, 110) == {"delta_pct": -5.0, "verdict": "early_cost"}   # you < plan
    assert ad.exit_delta(100, 96, 90) == {"delta_pct": 6.0, "verdict": "early_saved"}     # you > plan (dodged stop)
    assert ad.exit_delta(100, 110.2, 110)["verdict"] == "followed"                        # within eps
    assert ad.exit_delta(0, 1, 2)["verdict"] == "unknown"
    assert ad.exit_delta(100, None, 110)["verdict"] == "unknown"


def test_pct_and_compound():
    assert ad._pct(100, 110) == 10.0 and ad._pct(None, 110) is None
    assert ad._compound([10, -50]) == -45.0        # 1.10 × 0.50 − 1, not the +(−40) sum
    assert ad._compound([]) is None


def test_build_row_holding_is_followed():
    r = ad.build_row(ticker="A", horizon="short", source="tracked", entry=100,
                     your_exit=104, plan_exit=104, plan_reason=None, acted_out=False)
    assert r["verdict"] == "followed" and r["delta_pct"] == 0.0 and r["acted_out"] is False


def test_scorecard_aggregates_cost_saved_and_books():
    rows = [
        ad.build_row(ticker="A", horizon="s", source="tracked", entry=100, your_exit=105,
                     plan_exit=110, plan_reason="target", acted_out=True),   # −5 cost
        ad.build_row(ticker="B", horizon="s", source="tracked", entry=100, your_exit=96,
                     plan_exit=90, plan_reason="stop", acted_out=True),      # +6 saved
        ad.build_row(ticker="C", horizon="s", source="tracked", entry=100, your_exit=103,
                     plan_exit=103, plan_reason=None, acted_out=False),      # holding
    ]
    sc = ad.scorecard(rows)
    assert sc["n"] == 3 and sc["acted"] == 2 and sc["followed"] == 1
    assert sc["early_cost_n"] == 1 and sc["early_saved_n"] == 1
    assert sc["cost_pct"] == -5.0 and sc["saved_pct"] == 6.0
    assert sc["your_return_pct"] == pytest.approx(round((1.05 * 0.96 * 1.03 - 1) * 100, 2))
    assert sc["plan_return_pct"] == pytest.approx(round((1.10 * 0.90 * 1.03 - 1) * 100, 2))


def test_horizon_from_source():
    assert ad._horizon_from_source("idea-short") == "short"
    assert ad._horizon_from_source("basket") is None and ad._horizon_from_source(None) is None


# ── Orchestrator: explicit (tracked) path ───────────────────────────

def test_discipline_scorecard_tracked_path(monkeypatch):
    from pipeline_core.backtest import runner
    from pipeline_core.services import run_store

    async def fake_status(mk):
        # User bailed at 104; the plan ran on to its 110 target → an early exit that cost 6%.
        return {"open": [], "closed": [
            {"ticker": "WIN", "horizon": "short", "closed": True, "entry_price": 100.0,
             "exit_price": 110.0, "exit_reason": "target",
             "user_action": {"entered": True, "exited": True, "exit_price": 104.0}},
            {"ticker": "NOACT", "horizon": "short", "closed": True, "entry_price": 100.0,
             "exit_price": 108.0, "exit_reason": "target"},               # no user_action → ignored
        ]}

    async def no_paper(mk, user_id=None):
        return None

    monkeypatch.setattr(runner, "tracked_ideas_status", fake_status)
    monkeypatch.setattr(run_store, "get_paper", no_paper)
    out = asyncio.run(ad.discipline_scorecard("US"))
    assert len(out["rows"]) == 1 and out["rows"][0]["ticker"] == "WIN"
    assert out["rows"][0]["verdict"] == "early_cost" and out["rows"][0]["delta_pct"] == -6.0
    assert out["scorecard"]["early_cost_n"] == 1


# ── Orchestrator: paper path ────────────────────────────────────────

def test_discipline_scorecard_paper_path(monkeypatch):
    from pipeline_core.backtest import runner
    from pipeline_core.services import run_store

    async def empty_status(mk):
        return {"open": [], "closed": []}

    async def paper_acct(mk, user_id=None):
        return {"market": "US", "positions": [], "closed": [
            {"ticker": "EARLY", "source": "idea-short", "entry_price": 100.0, "stop": 94.0,
             "target": 112.0, "opened_at": "2026-06-01T00:00:00+00:00", "exit_price": 103.0,
             "reason": "manual"},                                          # bailed at 103…
            {"ticker": "FOLLOWED", "source": "idea-short", "entry_price": 100.0, "stop": 94.0,
             "target": 112.0, "opened_at": "2026-06-01T00:00:00+00:00", "exit_price": 112.0,
             "reason": "target"},                                          # …auto-closed = followed
            {"ticker": "MANUAL", "source": "manual", "entry_price": 100.0, "stop": 94.0,
             "target": 112.0, "opened_at": "2026-06-01T00:00:00+00:00", "exit_price": 90.0,
             "reason": "manual"},                                          # non-idea source → ignored
        ]}

    monkeypatch.setattr(runner, "tracked_ideas_status", empty_status)
    monkeypatch.setattr(run_store, "get_paper", paper_acct)
    monkeypatch.setattr(runner, "get_price_history", lambda tk, period, interval, mkt: "df")
    monkeypatch.setattr(runner, "_evaluate_idea_path",   # …the plan would have hit its 112 target
                        lambda idea, df: {"exit_price": 112.0, "exit_reason": "target", "bars_held": 5})
    monkeypatch.setattr(runner, "_idea_age_days", lambda idea: 60)
    monkeypatch.setattr(runner, "_period_for_days", lambda d: "3mo")

    out = asyncio.run(ad.discipline_scorecard("US"))
    by = {r["ticker"]: r for r in out["rows"]}
    assert set(by) == {"EARLY", "FOLLOWED"}                    # 'manual'-source trade excluded
    assert by["EARLY"]["verdict"] == "early_cost" and by["EARLY"]["delta_pct"] == -9.0   # 103 vs 112
    assert by["FOLLOWED"]["verdict"] == "followed"            # auto stop/target close = let it run
    assert out["scorecard"]["early_cost_n"] == 1 and out["scorecard"]["followed"] == 1
