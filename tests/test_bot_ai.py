"""AI layer for bots — LLM risk review with a rules-only fallback + a bounded,
de-risk-only overlay. Network-free (no LLM configured → template; LLM path mocked)."""

import asyncio

import pytest

from pipeline_core.bots import ai
from pipeline_core.bots.ai import BotReview, RiskFlag


def _risk_on():
    return {
        "regime": "risk-on", "regime_detail": "BTC > 200-DMA", "universe": "Top 50", "cash_pct": 0.0,
        "basket": [
            {"ticker": "ZEC", "weight": 0.24, "return_30d_pct": 12, "return_90d_pct": 41, "volatility_pct": 160},
            {"ticker": "TRX", "weight": 0.20, "return_30d_pct": 3, "return_90d_pct": 3, "volatility_pct": 28},
            {"ticker": "ETH", "weight": 0.20, "return_30d_pct": 1, "return_90d_pct": 1, "volatility_pct": 66},
        ],
    }


# ── Rules-only fallback (no LLM configured) ─────────────────────────


def test_review_rules_only_flags_high_vol():
    r = asyncio.run(ai.review_signal(_risk_on()))
    assert r["source"] == "rules"                    # no LLM → deterministic template
    assert r["briefing"] and r["conviction"] in ("proceed", "caution", "reduce")
    flags = {f["ticker"]: f["severity"] for f in r["risks"]}
    assert flags.get("ZEC") == "high"                # 160% vol → high severity → caution
    assert r["conviction"] == "caution"


def test_review_risk_off_is_reduce():
    r = asyncio.run(ai.review_signal({"regime": "risk-off", "cash_pct": 1.0, "basket": []}))
    assert r["conviction"] == "reduce" and r["risks"] == []


# ── De-risk overlay (bounded, cash-only) ────────────────────────────


def test_apply_review_derisks_high_severity_to_cash():
    sig = _risk_on()
    review = {"risks": [{"ticker": "ZEC", "severity": "high"}, {"ticker": "TRX", "severity": "medium"}]}
    out = ai.apply_review(sig, review)
    w = {b["ticker"]: b["weight"] for b in out["basket"]}
    assert w["ZEC"] == 0.12                           # high → halved
    assert w["TRX"] == 0.20                           # medium → untouched
    assert out["cash_pct"] > sig["cash_pct"]          # freed weight → cash (de-risk only)
    assert out["ai_applied"]["trimmed"] == ["ZEC"]


def test_apply_review_noop_without_high_flags():
    sig = _risk_on()
    out = ai.apply_review(sig, {"risks": [{"ticker": "ETH", "severity": "medium"}]})
    assert out is sig                                 # nothing high → unchanged


@pytest.mark.asyncio
async def test_annotate_attaches_and_optionally_applies():
    sig = _risk_on()
    plain = await ai.annotate(sig, apply=False)
    assert "ai" in plain and "ai_applied" not in plain
    applied = await ai.annotate(sig, apply=True)
    assert applied["ai_applied"]["trimmed"] == ["ZEC"]   # ZEC is high-vol → high severity


# ── LLM path (mocked) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_llm_path_normalizes(monkeypatch):
    async def fake_structured(prompt, schema, **kw):
        return BotReview(briefing="Momentum looks stretched.", conviction="PROCEED ",
                         risks=[RiskFlag(ticker="ETH", note="fine", severity="low")])

    monkeypatch.setattr(ai.llm_service, "llm_structured", fake_structured)
    monkeypatch.setattr(ai.llm_service, "llm_mode", lambda: "openai")

    r = await ai.review_signal(_risk_on())
    assert r["source"] == "llm" and r["llm_mode"] == "openai"
    assert r["conviction"] == "proceed"              # normalized from "PROCEED "
    assert r["risks"][0]["ticker"] == "ETH"


@pytest.fixture
def anyio_backend():
    return "asyncio"
