"""AI portfolio reviewer — rules-only construction/risk flags with an LLM path.
Network-free (no LLM configured → template; LLM path mocked)."""

import asyncio

import pytest

from pipeline_core.portfolio import ai
from pipeline_core.portfolio.ai import PortfolioReview, RiskNote


def _pf(**over):
    base = dict(
        name="P", cash_pct=0.0,
        spec={"market": "IN", "basis": "blend", "risk": "balanced", "weighting": "score", "theme": None},
        summary={"position_count": 5, "top3_concentration": 0.72, "sector_count": 1,
                 "avg_conviction": 2.0, "weighted_win_rate": 42, "weighted_sharpe": 0.8,
                 "allocation_by_sector": {"Technology": 1.0}},
        positions=[{"ticker": "A", "sector": "Technology", "weight": 0.35, "conviction": 3},
                   {"ticker": "B", "sector": "Technology", "weight": 0.25, "conviction": 2}],
    )
    base.update(over)
    return base


# ── Rules-only fallback ─────────────────────────────────────────────


def test_flags_concentration_sector_and_edge():
    r = asyncio.run(ai.review_portfolio(_pf()))
    assert r["source"] == "rules" and r["read"]
    areas = {x["area"] for x in r["risks"]}
    assert "concentration" in areas and "sector" in areas and "edge" in areas
    sev = {x["area"]: x["severity"] for x in r["risks"]}
    assert sev["concentration"] == "high"                    # top-3 = 72%


def test_diversified_book_has_strengths():
    pf = _pf(cash_pct=0.15,
             summary={"position_count": 10, "top3_concentration": 0.30, "sector_count": 6,
                      "avg_conviction": 2.0, "weighted_win_rate": 60, "weighted_sharpe": 1.2,
                      "allocation_by_sector": {"Tech": 0.3, "Fin": 0.3, "Health": 0.2, "Energy": 0.2}})
    r = asyncio.run(ai.review_portfolio(pf))
    joined = " ".join(r["strengths"]).lower()
    assert "sector" in joined or "win rate" in joined
    assert not any(x["area"] == "concentration" for x in r["risks"])   # top-3 30% is fine


# ── LLM path (mocked) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_path_normalizes_severity(monkeypatch):
    async def fake_structured(prompt, schema, **kw):
        return PortfolioReview(read="Balanced.", strengths=["diversified"],
                               risks=[RiskNote(area="cash", note="no dry powder", severity="HIGH ")])

    monkeypatch.setattr(ai.llm_service, "llm_structured", fake_structured)
    monkeypatch.setattr(ai.llm_service, "llm_mode", lambda: "openai")
    r = await ai.review_portfolio(_pf())
    assert r["source"] == "llm" and r["llm_mode"] == "openai"
    assert r["risks"][0]["severity"] == "high"               # normalized from "HIGH "


@pytest.fixture
def anyio_backend():
    return "asyncio"
