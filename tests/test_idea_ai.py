"""AI analyst for trade ideas — verdict blending + post-mortem, with a rules-only
fallback. Network-free (no LLM configured → template; LLM path & sentiment mocked)."""

import asyncio

import pytest

from pipeline_core.backtest import idea_ai
from pipeline_core.backtest.idea_ai import IdeaBriefing


def _idea(**over):
    base = dict(ticker="ABC", horizon="medium", price=100.0, buy_low=97.0, buy_high=103.0,
                target_price=118.0, target_pct=0.18, stop_price=91.0, stop_pct=0.09,
                risk_reward=2.0, conviction=3, avg_win_rate=58, age_days=1,
                strategies=[{"name": "Golden Cross", "win_rate": 60}])
    base.update(over)
    return base


# ── Rules-only verdict blending (no LLM configured) ─────────────────


def test_strong_quant_is_buy():
    r = asyncio.run(idea_ai.analyze_idea(_idea(), "IN", with_sentiment=False))
    assert r["source"] == "rules" and r["verdict"] == "buy"
    assert r["thesis"] and r["key_risk"] and "91" in r["invalidation"]


def test_weak_quant_is_avoid():
    weak = _idea(conviction=1, avg_win_rate=40, risk_reward=0.8)
    r = asyncio.run(idea_ai.analyze_idea(weak, "IN", with_sentiment=False))
    assert r["verdict"] == "avoid"


def test_bearish_sentiment_downgrades_verdict(monkeypatch):
    async def fake_sent(ticker, name, market):
        return {"score": 20.0, "consensus": "Bearish", "summary": "rough tape",
                "articles": [1, 2], "method": "lexicon"}

    monkeypatch.setattr(idea_ai.sentiment_agent, "analyze_ticker_sentiment", fake_sent)
    r = asyncio.run(idea_ai.analyze_idea(_idea(), "IN"))          # strong quant (4) − bearish (2) = watch
    assert r["verdict"] == "watch"
    assert r["sentiment"]["consensus"] == "Bearish" and r["sentiment"]["article_count"] == 2


def test_extended_price_is_penalized():
    # Price has run well above the buy zone → the extension penalty knocks it off "buy".
    r = asyncio.run(idea_ai.analyze_idea(_idea(price=130.0), "IN", with_sentiment=False))
    assert r["verdict"] == "watch" and "buy zone" in r["thesis"].lower()


# ── LLM path (mocked) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_path_normalizes_verdict(monkeypatch):
    async def fake_structured(prompt, schema, **kw):
        return IdeaBriefing(thesis="Momentum aligned.", verdict="BUY ",
                            key_risk="regime", invalidation="below stop")

    monkeypatch.setattr(idea_ai.llm_service, "llm_structured", fake_structured)
    monkeypatch.setattr(idea_ai.llm_service, "llm_mode", lambda: "anthropic")
    r = await idea_ai.analyze_idea(_idea(), "IN", with_sentiment=False)
    assert r["source"] == "llm" and r["verdict"] == "buy" and r["llm_mode"] == "anthropic"


# ── Post-mortem over the realized record ────────────────────────────


def test_postmortem_flags_more_stops_than_targets():
    status = {"closed_scoreboard": {"closed": 6, "wins": 2, "losses": 4, "targets_hit": 2,
                                    "stopped": 4, "expired": 0, "win_rate": 33.3,
                                    "avg_return_pct": -1.0, "total_return_pct": -6.0},
              "closed": [{"ticker": "A", "horizon": "short", "exit_reason": "stop",
                          "realized_return_pct": -5}], "open": []}
    r = asyncio.run(idea_ai.analyze_track_record(status))
    assert r["source"] == "rules" and r["summary"]
    assert any("stop" in ln.lower() for ln in r["lessons"])


def test_postmortem_empty_record():
    r = asyncio.run(idea_ai.analyze_track_record({"closed_scoreboard": {"closed": 0},
                                                 "closed": [], "open": []}))
    assert r["lessons"] == [] and "No trades" in r["summary"]


@pytest.fixture
def anyio_backend():
    return "asyncio"
